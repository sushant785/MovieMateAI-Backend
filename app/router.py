import time
import re
import json
import os
from fastapi import APIRouter, Depends, HTTPException
from .schemas import ChatRequest, ChatResponse, Movie
from .ai_service import app as agent_app
from database import get_db_connection
from .dependencies import get_optional_user
from pydantic import BaseModel
import httpx 

TMDB_API_KEY = os.getenv("TMDB_API_KEY")


router = APIRouter()

class WatchlistUpdate(BaseModel):
    movie_title: str

class MovieActionRequest(BaseModel):
    movie_title: str

class MovieObjectRequest(BaseModel):
    movie: dict



@router.get("/my_list")
async def get_watchlist(user_id: str | None = Depends(get_optional_user)):
    if not user_id:
        raise HTTPException(status_code=401, detail="Log in to view your list.")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        with conn.cursor() as cursor:
            # Fetch the my_list array for this specific user
            query = "SELECT my_list FROM profiles WHERE user_id = %s;"
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            
            # If the user exists and has a list, return it. Otherwise, return an empty array.
            watchlist = result.get('my_list') if result and result.get('my_list') else []
            
        return {"status": "success", "watchlist": watchlist}
        
    except Exception as e:
        print(f"Database error fetching watchlist: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch watchlist")
    finally:
        conn.close()





@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    req: ChatRequest, 
    user_id: str | None = Depends(get_optional_user) # 1. The Smart Gatekeeper
):

    thread_id = "guest_session"
    user_context = (
    "You are chatting with a Guest User. You do not know their streaming platforms or preferences. "
    "To provide a great recommendation, you need to know their platforms (Netflix, Prime, Hotstar, etc.), "
    "their current mood, and preferred language. "
    "CRITICAL: Ask for these details ONE AT A TIME to keep the conversation natural. "
    "Start by politely asking which streaming services they use. "
    "Do not provide any movie recommendations until you have confirmed at least one platform and their current mood."
    "CRITICAL: When you finally have enough information to make recommendations, ALWAYS suggest at least 2 movies at a time."
)
    db_watched_list = req.watched_list # Fallback to whatever the frontend sent

    if user_id:
        thread_id = user_id 
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT subscriptions, watched_list FROM profiles WHERE user_id = %s", 
                (user_id,)
            )
            profile = cur.fetchone()
            
            if profile:
                # Extract Platforms
                subs = profile['subscriptions']
                active_platforms = [p for p, val in subs.items() if val]
                
                if active_platforms:
                    platforms_str = ', '.join(active_platforms)
                    user_context = (
                        f"Logged-in User. They have access to: {platforms_str}. "
                        f"DO NOT ask the user to choose a platform; just pick from their list. "
                        f"Before recommending, check if the user specified a mood, genre, or language. "
                        f"If they haven't provided these details, ask for ONE thing at a time to keep it conversational. "
                        f"For example, if the mood is missing, ask: 'What kind of mood are you in tonight?' "
                        f"DO NOT ask for mood, genre, and language all in one message. "
                        f"Once you have enough context to make a great pick, give the recommendations immediately. "
                        f"Never suggest movies from platforms not on their list."
                        f"CRITICAL: When you provide recommendations, ALWAYS suggest at least 2 movies at a time."
                    )
                else:
                    user_context = "Logged-in User. They have no streaming platforms selected. Warn them to update their settings."
                
                # Extract Watched List
                db_watched_list = profile.get('watched_list', [])

        except Exception as e:
            print(f"DB Error fetching profile: {e}")
        finally:
            cur.close()
            conn.close()

    # 3. Setup LangGraph Configuration
    config = {"configurable": {"thread_id": thread_id}}
    
    # 4. Inject the Database Context into the AI's prompt
    messages_to_send = [
        ("system", user_context), # This acts as a secret instruction for this specific turn
        ("user", req.message)
    ]

    # 5. Invoke LangGraph
    result_state = await agent_app.ainvoke(
        {
            "messages": messages_to_send,
            "watched_list": db_watched_list # Passing the DB list instead of the frontend list!
        },
        config=config
    )
    

    # SAFELY Extract the final AI reply
    raw_content = result_state["messages"][-1].content
    final_message = ""
    
    if isinstance(raw_content, str):
        final_message = raw_content
    elif isinstance(raw_content, list):
        for block in raw_content:
            if isinstance(block, dict) and 'text' in block:
                final_message += block['text']
            elif isinstance(block, str):
                final_message += block

    # Clean up markdown and links
    final_message = re.sub(r'!\[.*?\]\(.*?\)', '', final_message)
    final_message = re.sub(r'http[s]?://\S+', '', final_message)
    final_message = final_message.replace('**', '')

    recommendations = []
    
    # Loop backwards to find tool messages for movie objects
    for msg in reversed(result_state["messages"]):
        if msg.type in ["human", "user"]:
            break
            
        if msg.type == "tool" and isinstance(msg.content, str):
            try:
                movie_data = json.loads(msg.content)
                if "id" in movie_data:
                    recommendations.insert(0, Movie(**movie_data))
            except:
                pass 

    # Send back to React Native
    return ChatResponse(
        id=str(int(time.time() * 1000)),
        content=final_message.strip(),
        recommendations=recommendations
    )


@router.post("/my_list/add")
async def add_to_my_list(
    req: MovieObjectRequest, 
    user_id: str | None = Depends(get_optional_user)
):
    print(f"--- DEBUG: Saving Full Movie Object to My List ---")
    if not user_id:
        raise HTTPException(status_code=401, detail="Log in to save movies.")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        with conn.cursor() as cursor:
            movie_json = json.dumps([req.movie])
            title = req.movie.get("title") 

            # Added COALESCE to protect against NULL arrays
            query = """
                UPDATE profiles
                SET my_list = COALESCE(my_list, '[]'::jsonb) || %s::jsonb
                WHERE user_id = %s
                AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(COALESCE(my_list, '[]'::jsonb)) AS elem 
                    WHERE elem->>'title' = %s
                );
            """
            cursor.execute(query, (movie_json, user_id, title))
            rows_updated = cursor.rowcount
            conn.commit()
            
        if rows_updated == 0:
            return {"status": "ignored", "message": "Movie already saved."}
            
        return {"status": "success", "message": f"'{title}' added."}
        
    except Exception as e:
        print(f"Detailed Database error: {e}")
        conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to add to watchlist")
    finally:
        conn.close()


@router.get("/discover")
async def get_discover_movies():
    try:
        # We use httpx to make the external API call from the server
        async with httpx.AsyncClient() as client:
            trending_res = await client.get(f"https://api.themoviedb.org/3/trending/movie/day?api_key={TMDB_API_KEY}")
            now_playing_res = await client.get(f"https://api.themoviedb.org/3/movie/now_playing?api_key={TMDB_API_KEY}")
            
            # Send it straight back to the frontend
            return {
                "status": "success",
                "trending": trending_res.json().get("results", []),
                "now_playing": now_playing_res.json().get("results", [])
            }
    except Exception as e:
        print(f"Backend TMDB Fetch Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch from TMDB")


class SeenMovieRequest(BaseModel):
    movie_id: str
    title: str

@router.post("/watchlist/seen")
async def mark_movie_as_seen(
    req: SeenMovieRequest, 
    user_id: str | None = Depends(get_optional_user)
):
    print(f"--- DEBUG: Marking movie as seen: {req.title} ---")
    if not user_id:
        raise HTTPException(status_code=401, detail="Log in to manage your list.")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT my_list, watched_list FROM profiles WHERE user_id = %s FOR UPDATE;", 
                (user_id,)
            )
            profile = cursor.fetchone()
            
            if not profile:
                raise HTTPException(status_code=404, detail="Profile not found")

            my_list = profile.get('my_list') or []
            watched_list = profile.get('watched_list') or []

            updated_my_list = [m for m in my_list if str(m.get('id')) != str(req.movie_id)]

            if req.title not in watched_list:
                watched_list.append(req.title)

                update_query = """
                UPDATE profiles 
                SET my_list = %s::jsonb, 
                    watched_list = %s 
                WHERE user_id = %s;
            """
            cursor.execute(
                update_query, 
                (json.dumps(updated_my_list), watched_list, user_id)
            )
            
            conn.commit()
            return {"status": "success", "message": f"'{req.title}' moved to watched list."}

    except Exception as e:
        print(f"Detailed Database error in /watchlist/seen: {e}")
        conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to update database")
    finally:
        conn.close()


@router.get("/movie/{movie_id}/trailer")
async def get_movie_trailer(movie_id: str):
    try:
        async with httpx.AsyncClient() as client:
            # Fetch videos from TMDB
            url = f"https://api.themoviedb.org/3/movie/{movie_id}/videos?api_key={TMDB_API_KEY}"
            res = await client.get(url)
            data = res.json()
            
            results = data.get("results", [])
            trailer = next((v for v in results if v["type"] == "Trailer" and v["site"] == "YouTube"), None)
            
            if not trailer:
                trailer = next((v for v in results if v["site"] == "YouTube"), None)

            if trailer:
                return {"youtube_key": trailer["key"]}
            
            raise HTTPException(status_code=404, detail="Trailer not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))