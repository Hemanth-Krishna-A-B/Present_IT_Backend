from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from uuid import uuid4
import shutil
import os
import subprocess
from pdf2image import convert_from_path
from pptx import Presentation
from PIL import Image
from supabase import create_client
from dotenv import load_dotenv
import logging
import asyncio
from pathlib import Path

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL or Key not set in the environment variables")

# Initialize Supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize FastAPI app
app = FastAPI()

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set up logging
logging.basicConfig(level=logging.INFO)

# Upload directory
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


def save_temp_file(uploaded_file: UploadFile) -> str:
    """Save uploaded file temporarily."""
    safe_name = Path(uploaded_file.filename).name  # Prevent path traversal
    file_path = os.path.join(UPLOAD_DIR, f"{uuid4()}_{safe_name}")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(uploaded_file.file, buffer)
    return file_path


def convert_pdf_to_images(pdf_path: str) -> list:
    """Convert PDF to images."""
    try:
        images = convert_from_path(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error converting PDF to images: {e}")
    
    image_paths = []
    for idx, image in enumerate(images):
        img_path = f"{pdf_path}_{idx}.png"
        image.save(img_path, "PNG")
        image_paths.append(img_path)
    return image_paths


def convert_pptx_to_images(pptx_path: str) -> list:
    """Convert PPTX to images via PDF using LibreOffice."""
    try:
        pdf_path = pptx_path.replace(".pptx", ".pdf")
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", pptx_path, "--outdir", UPLOAD_DIR],
            check=True,
        )
        return convert_pdf_to_images(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error converting PPTX to images: {e}")


def upload_images_to_supabase(image_paths: list, folder_id: str) -> list:
    """Upload images to Supabase and return their public URLs."""
    folder_path = f"images/{folder_id}/"
    public_urls = []

    for img_path in image_paths:
        img_name = os.path.basename(img_path)
        dest_path = f"{folder_path}{img_name}"

        with open(img_path, "rb") as img_file:
            response = supabase.storage.from_("presentations").upload(dest_path, img_file)
            if response.get("error"):
                raise HTTPException(status_code=500, detail=f"Upload failed: {response['error']['message']}")

        public_url = f"{SUPABASE_URL}/storage/v1/object/public/presentations/{dest_path}"
        public_urls.append(public_url)

        os.remove(img_path)  # Remove temporary image file

    return public_urls


@app.post("/upload/")
async def upload_file(file: UploadFile = File(...)):
    """
    Upload a PDF or PPTX file, convert it to images, upload to Supabase,
    and return public image URLs.
    """
    if file.content_type not in [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ]:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # Save uploaded file
    file_path = save_temp_file(file)
    folder_id = str(uuid4())

    # Convert file to images in a thread-safe way
    loop = asyncio.get_event_loop()
    try:
        if file.content_type == "application/pdf":
            image_paths = await loop.run_in_executor(None, convert_pdf_to_images, file_path)
        else:
            image_paths = await loop.run_in_executor(None, convert_pptx_to_images, file_path)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)  # Clean up the original file

    # Upload images to Supabase
    public_urls = upload_images_to_supabase(image_paths, folder_id)

    logging.info(f"Uploaded and processed {file.filename}. URLs: {public_urls}")
    return JSONResponse(content={"image_urls": public_urls})
