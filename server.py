from fastapi import FastAPI, Query, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv
from uuid import uuid4
from pathlib import Path
import pandas as pd
import tempfile
import shutil
import os
import subprocess
import logging
import asyncio
from pdf2image import convert_from_path
from pptx import Presentation
from PIL import Image

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = SUPABASE_KEY  # You can separate keys if needed

# Check env
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase credentials not set.")

# Init Supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Init app
app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
logging.basicConfig(level=logging.INFO)

# Constants
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------- Helper Functions ----------

def save_temp_file(uploaded_file: UploadFile) -> str:
    safe_name = Path(uploaded_file.filename).name
    file_path = os.path.join(UPLOAD_DIR, f"{uuid4()}_{safe_name}")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(uploaded_file.file, buffer)
    return file_path

def convert_pdf_to_images(pdf_path: str) -> list:
    try:
        images = convert_from_path(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF conversion failed: {e}")
    image_paths = []
    for idx, image in enumerate(images):
        img_path = f"{pdf_path}_{idx}.png"
        image.save(img_path, "PNG")
        image_paths.append(img_path)
    return image_paths

def convert_pptx_to_images(pptx_path: str) -> list:
    try:
        pdf_path = pptx_path.replace(".pptx", ".pdf")
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", pptx_path, "--outdir", UPLOAD_DIR],
            check=True,
        )
        return convert_pdf_to_images(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PPTX conversion failed: {e}")

def upload_images_to_supabase(image_paths: list, folder_id: str) -> list:
    folder_path = f"images/{folder_id}/"
    public_urls = []
    for img_path in image_paths:
        img_name = os.path.basename(img_path)
        dest_path = f"{folder_path}{img_name}"
        with open(img_path, "rb") as img_file:
            try:
                supabase.storage.from_("presentations").upload(dest_path, img_file)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Image upload failed: {e}")
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/presentations/{dest_path}"
        public_urls.append(public_url)
        os.remove(img_path)
    return public_urls

# ---------- API Endpoints ----------

@app.post("/generate-report")
def generate_report(session_id: int = Query(...)):

    # Deactivate session
    try:
        update_response = supabase.table("session").update({
            "active": False
        }).eq("id", session_id).execute()

        if not update_response.data:
            raise HTTPException(status_code=404, detail="Session update failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update session: {str(e)}")
    
    # Fetch report data using RPC
    try:
        response = supabase.rpc("get_session_report", {"session_id_input": session_id}).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase RPC error: {str(e)}")
    
    if not response.data:
        raise HTTPException(status_code=404, detail="No data found for this session")

    # Convert to CSV
    df = pd.DataFrame(response.data)
    csv_str = df.to_csv(index=False)
    filename = f"report_session_{session_id}.csv"
    file_path = f"reports/{filename}"

    # Upload CSV to Supabase storage correctly
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".csv", delete=True) as temp_file:
        temp_file.write(csv_str)
        temp_file.flush()
        with open(temp_file.name, "rb") as f:
            try:
                supabase.storage.from_("reports").upload(file_path, f)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    # Get public URL of uploaded report
    try:
        public_url = supabase.storage.from_("reports").get_public_url(file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get public URL: {str(e)}")

    # Update session with report URL
    try:
        update_response = supabase.table("session").update({
            "report_url": public_url,
        }).eq("id", session_id).execute()

        if not update_response.data:
            raise HTTPException(status_code=404, detail="Session update failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update session: {str(e)}")

    # Cleanup related tables
    try:
        supabase.table("poll-response").delete().eq("session_id", session_id).execute()
    except Exception as e:
        logging.warning("Error deleting poll-response: " + str(e))
    try:
        supabase.table("attendance").delete().eq("session_id", session_id).execute()
    except Exception as e:
        logging.warning("Error deleting attendance: " + str(e))
    try:
        supabase.table("leaderboard").delete().eq("session_id", session_id).execute()
    except Exception as e:
        logging.warning("Error deleting leaderboard: " + str(e))

    return {
        "message": "Report uploaded and session updated",
        "public_url": public_url,
        "rows": len(df)
    }


@app.post("/upload/")
async def upload_file(file: UploadFile = File(...)):
    logging.info(f"Received file: {file.filename} ({file.content_type})")

    valid_types = [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ]
    if file.content_type not in valid_types:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    file_path = save_temp_file(file)
    folder_id = str(uuid4())

    loop = asyncio.get_event_loop()
    try:
        if file.content_type == "application/pdf":
            image_paths = await loop.run_in_executor(None, convert_pdf_to_images, file_path)
        else:
            image_paths = await loop.run_in_executor(None, convert_pptx_to_images, file_path)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    public_urls = upload_images_to_supabase(image_paths, folder_id)
    logging.info(f"Uploaded images: {public_urls}")
    return JSONResponse(content={"image_urls": public_urls})
    

@app.post("/cleanup-unused-sessions")
def cleanup_unused_sessions(user_id: str = Query(..., description="User UUID to clean sessions for")):
    try:
        sessions_response = supabase.table("session").select("*").eq("active", True).eq("teacher_id", user_id).execute()
        active_sessions = sessions_response.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch active sessions: {str(e)}")

    if not active_sessions:
        return {"message": f"No active sessions found for user {user_id} to clean up."}

    cleaned = []

    for session in active_sessions:
        session_id = session.get("id")
        try:
            report_response = generate_report(session_id=session_id)
            cleaned.append({
                "session_id": session_id,
                "report_url": report_response["public_url"],
                "rows": report_response["rows"]
            })
        except Exception as e:
            logging.warning(f"Failed to clean session {session_id}: {str(e)}")

    return {
        "message": f"{len(cleaned)} sessions cleaned up and reports generated for user {user_id}.",
        "sessions": cleaned
    }
