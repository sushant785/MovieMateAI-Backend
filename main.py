from fastapi import FastAPI, HTTPException, status, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from database import get_db_connection
from pydantic import BaseModel, EmailStr
import security
import json
from jose import jwt, JWTError
from app.dependencies import get_optional_user
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import os

# Load env vars before importing routers
load_dotenv()

from app.router import router

app = FastAPI(title="MovieMate AI Backend")

# Allow your React Native app to communicate with this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserCredentials(BaseModel):
    email: EmailStr
    password: str

class SubscriptionsUpdate(BaseModel):
    netflix: bool
    hotstar: bool
    prime: bool

@app.post("/api/signup", status_code=status.HTTP_201_CREATED)
def signup(user: UserCredentials):
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
    
    try:
        cur = conn.cursor()
        
        # Hash the password before saving it!
        hashed_pw = security.hash_password(user.password)
        
        # Insert the user into Supabase and return their new ID
        cur.execute(
            """
            INSERT INTO users (email, password_hash) 
            VALUES (%s, %s) 
            RETURNING id, email;
            """,
            (user.email, hashed_pw)
        )
        new_user = cur.fetchone()        
        conn.commit()
        
        return {"message": "User created successfully!", "user": new_user}

    except Exception as e:
        # If the email already exists, Postgres throws a UniqueViolation error
        conn.rollback()
        raise HTTPException(status_code=400, detail="Email already registered or database error.")
    finally:
        cur.close()
        conn.close()


@app.post("/api/login")
def login(user: UserCredentials):
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")
        
    try:
        cur = conn.cursor()
        
        # Find the user by email
        cur.execute(
            "SELECT id, email, password_hash FROM users WHERE email = %s;",
            (user.email,)
        )
        db_user = cur.fetchone()
        
        # Check if user exists AND if the password matches the hash
        if not db_user or not security.verify_password(user.password, db_user['password_hash']):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
            
        # Create the JWT token using the user's ID
        access_token = security.create_access_token(data={"sub": str(db_user['id'])})
        
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "user": {"id": db_user['id'], "email": db_user['email']}
        }
        
    finally:
        cur.close()
        conn.close()

    
@app.get("/api/profile")
def get_profile(user_id: str | None = Depends(get_optional_user)):
    # 1. Block guests from hitting this endpoint
    if not user_id:
        raise HTTPException(status_code=401, detail="Must be logged in to view profile")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 2. Look for the user's profile
        cur.execute("SELECT subscriptions FROM profiles WHERE user_id = %s;", (user_id,))
        profile = cur.fetchone()

        # 3. If they just signed up, they won't have a profile row yet! Let's create a default one.
        if not profile:
            cur.execute(
                """
                INSERT INTO profiles (user_id) 
                VALUES (%s) 
                RETURNING subscriptions;
                """, 
                (user_id,)
            )
            profile = cur.fetchone()
            conn.commit()

        return {"subscriptions": profile['subscriptions']}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.put("/api/profile")
def update_profile(subs: SubscriptionsUpdate, user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        raise HTTPException(status_code=401, detail="Must be logged in to update profile")

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        subs_json = json.dumps(subs.model_dump()) 

        cur.execute(
            """
            UPDATE profiles 
            SET subscriptions = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE user_id = %s 
            RETURNING subscriptions;
            """,
            (subs_json, user_id)
        )
        updated_profile = cur.fetchone()

        if not updated_profile:
            cur.execute(
                """
                INSERT INTO profiles (user_id, subscriptions) 
                VALUES (%s, %s) 
                RETURNING subscriptions;
                """,
                (user_id, subs_json)
            )
            updated_profile = cur.fetchone()

        conn.commit()
        return {"message": "Preferences saved!", "subscriptions": updated_profile['subscriptions']}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
class GoogleAuthRequest(BaseModel):
    id_token: str


@app.post("/api/auth/google")
async def google_auth(payload: GoogleAuthRequest):
    # 1. Verify the Google Token
    try:
        idinfo = id_token.verify_oauth2_token(
            payload.id_token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=10
        )
        email = idinfo.get('email')
        if not email:
            raise HTTPException(status_code=400, detail="Email not provided by Google")
            
    except ValueError as e:
        print(f"GOOGLE TOKEN ERROR: {str(e)}") 
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {str(e)}")

    # 2. Database Operations using Cursor
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Check if user exists
        cur.execute("SELECT id, email FROM users WHERE email = %s", (email,))
        user_record = cur.fetchone()

        if not user_record:
            # NEW USER: Insert into the users table
            cur.execute(
                """
                INSERT INTO users (email, password_hash) 
                VALUES (%s, %s) 
                RETURNING id, email;
                """,
                (email, "GOOGLE_AUTH")
            )
            user_record = cur.fetchone()
        
        conn.commit()

        # Extract data safely (handles both dict and tuple returns)
        user_email = user_record['email'] if isinstance(user_record, dict) else user_record[1]
        user_id = user_record['id'] if isinstance(user_record, dict) else user_record[0]

        # 3. Generate JWT (CRITICAL FIX: Use str(user_id) to match your standard login!)
        access_token = security.create_access_token(
            data={"sub": str(user_id)}
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": str(user_id),
                "email": user_email
            }
        }

    except Exception as e:
        conn.rollback()
        print(f"Database Error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")
    finally:
        cur.close()
        conn.close()

app.include_router(router, prefix="/api")

if __name__ == "__main__":
    import uvicorn
    # Runs on port 8000
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)