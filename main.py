import uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure, PyMongoError
import resend
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from datetime import datetime, timezone
import os
import logging
import asyncio
from typing import Union
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(
    title="Portfolio Project Request API",
    description="API for submitting project and contact requests to Muhammad Ahmad's portfolio, storing data in MongoDB, and sending notifications via Resend.",
    version="1.0.0"
)

# CORS configuration
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8080").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI is not set in environment variables")
    raise ValueError("MONGO_URI is not set in environment variables")

# Initialize MongoDB client
client = None
db = None
collection = None

# Resend configuration
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
if not RESEND_API_KEY:
    logger.warning("RESEND_API_KEY not set. Email notifications will not work.")
else:
    resend.api_key = RESEND_API_KEY

# Pydantic models for request validation
class ProjectDetails(BaseModel):
    clientType: str | None = None
    clientName: str | None = None
    companyName: str | None = None
    projectType: str | None = None
    budget: str | None = None
    timeline: str | None = None
    requirements: str | None = None
    contactEmail: EmailStr

    class Config:
        json_schema_extra = {
            "example": {
                "clientType": "individual",
                "clientName": "John Doe",
                "companyName": None,
                "projectType": "Web App",
                "budget": "$5000",
                "timeline": "3 months",
                "requirements": "React and Node.js",
                "contactEmail": "john@example.com"
            }
        }

class ContactDetails(BaseModel):
    name: str
    email: EmailStr
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "name": "John Doe",
                "email": "john@example.com",
                "message": "Hello, I have a project idea to discuss."
            }
        }

# Helper function to validate project details
def validate_project_details(details: ProjectDetails) -> None:
    if not details.contactEmail:
        raise HTTPException(status_code=400, detail="Contact email is required")
    if details.clientType and details.clientType not in ["company", "individual"]:
        raise HTTPException(status_code=400, detail="Invalid client type")
    if details.clientType == "company" and not details.companyName:
        raise HTTPException(status_code=400, detail="Company name is required for company client type")
    if details.clientType == "individual" and not details.clientName:
        raise HTTPException(status_code=400, detail="Client name is required for individual client type")

# Helper function to validate contact details
def validate_contact_details(details: ContactDetails) -> None:
    if not details.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    if not details.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

# Retry decorator for MongoDB connection
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type(ConnectionFailure),
    before_sleep=lambda retry_state: logger.info(f"Retrying MongoDB connection (attempt {retry_state.attempt_number})...")
)
async def connect_to_mongodb():
    global client, db, collection
    client = AsyncIOMotorClient(MONGO_URI)
    await client.admin.command("ping")
    db = client["portfolio"]
    collection = db["project_requests"]
    logger.info("MongoDB connection established")

# Consolidated helper function to send email via Resend with retry
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: logger.info(f"Retrying email send (attempt {retry_state.attempt_number})...")
)
async def send_email(details: Union[ProjectDetails, ContactDetails]) -> bool:
    if not RESEND_API_KEY:
        logger.warning("Resend API key not set. Skipping email send.")
        return False

    try:
        if isinstance(details, ProjectDetails):
            email_content = {
                "from": "onboarding@resend.dev",
                "to": "ahmadrajpootr1@gmail.com",
                "subject": "New Project Request from AI Assistant",
                "html": (
                    f"<h3>New Project Request</h3>"
                    f"<p><strong>Client Type:</strong> {details.clientType or 'Not specified'}</p>"
                    f"<p><strong>{'Company Name' if details.clientType == 'company' else 'Client Name'}:</strong> "
                    f"{details.companyName or details.clientName or 'Not specified'}</p>"
                    f"<p><strong>Project Type:</strong> {details.projectType or 'Not specified'}</p>"
                    f"<p><strong>Budget:</strong> {details.budget or 'Not specified'}</p>"
                    f"<p><strong>Timeline:</strong> {details.timeline or 'Not specified'}</p>"
                    f"<p><strong>Requirements:</strong> {details.requirements or 'Not specified'}</p>"
                    f"<p><strong>Contact Email:</strong> {details.contactEmail}</p>"
                    f"<p><strong>Received At:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</p>"
                )
            }
        else:  # ContactDetails
            email_content = {
                "from": "onboarding@resend.dev",
                "to": "ahmadrajpootr1@gmail.com",
                "subject": "New Contact Form Submission",
                "html": (
                    f"<h3>New Contact Form Submission</h3>"
                    f"<p><strong>Name:</strong> {details.name}</p>"
                    f"<p><strong>Email:</strong> {details.email}</p>"
                    f"<p><strong>Message:</strong> {details.message}</p>"
                    f"<p><strong>Received At:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</p>"
                )
            }

        response = await asyncio.to_thread(resend.Emails.send, email_content)
        logger.info(f"Email sent successfully: Message ID {response['id']}")
        return True
    except Exception as e:
        logger.error(f"Resend error: {str(e)}")
        if "403" in str(e).lower():
            logger.error("Resend 403 Forbidden: Check API key permissions or sender verification")
        raise

# FastAPI startup event to initialize MongoDB connection
@app.on_event("startup")
async def startup_event():
    try:
        await connect_to_mongodb()
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB after retries: {e}")
        raise

# FastAPI shutdown event to close MongoDB client
@app.on_event("shutdown")
async def shutdown_event():
    global client
    if client:
        client.close()
        logger.info("MongoDB connection closed")

@app.post(
    "/api/project-request",
    summary="Submit a project request",
    description="Submit project or hiring details to Muhammad Ahmad. Stores data in MongoDB and sends an email notification.",
    response_description="Confirmation message and request ID"
)
async def submit_project_request(details: ProjectDetails):
    try:
        # Validate input data
        validate_project_details(details)

        # Prepare data for MongoDB
        project_data = details.model_dump(exclude_unset=True)
        project_data["created_at"] = datetime.now(timezone.utc)
        project_data["type"] = "project_request"

        # Store in MongoDB (async)
        result = await collection.insert_one(project_data)
        if not result.inserted_id:
            raise PyMongoError("Failed to store project details in MongoDB")

        # Send email notification
        email_sent = await send_email(details)
        if not email_sent:
            logger.warning("Email notification failed to send")

        return {
            "message": "Details sent to Muhammad Ahmad. He will contact you soon!",
            "request_id": str(result.inserted_id),
            "email_sent": email_sent
        }
    except HTTPException as e:
        raise e
    except PyMongoError as e:
        logger.error(f"MongoDB error: {e}")
        raise HTTPException(status_code=500, detail="Failed to store project details")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred. Please try again or contact Muhammad directly at ahmadrajpootr1@gmail.com."
        )

@app.post(
    "/api/contact",
    summary="Submit a contact form",
    description="Submit contact form details to Muhammad Ahmad. Stores data in MongoDB and sends an email notification.",
    response_description="Confirmation message and request ID"
)
async def submit_contact_request(details: ContactDetails):
    try:
        # Validate input data
        validate_contact_details(details)

        # Prepare data for MongoDB
        contact_data = details.model_dump()
        contact_data["created_at"] = datetime.now(timezone.utc)
        contact_data["type"] = "contact_request"

        # Store in MongoDB (async)
        result = await collection.insert_one(contact_data)
        if not result.inserted_id:
            raise PyMongoError("Failed to store contact details in MongoDB")

        # Send email notification
        email_sent = await send_email(details)
        if not email_sent:
            logger.warning("Email notification failed to send")

        return {
            "message": "Your message has been sent to Muhammad Ahmad. He will contact you soon!",
            "request_id": str(result.inserted_id),
            "email_sent": email_sent
        }
    except HTTPException as e:
        raise e
    except PyMongoError as e:
        logger.error(f"MongoDB error: {e}")
        raise HTTPException(status_code=500, detail="Failed to store contact details")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred. Please try again or contact Muhammad directly at ahmadrajpootr1@gmail.com."
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)