import os
import httpx
from typing import Optional
from langchain_core.tools import tool

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

@tool
async def fetch_movie_poster(title: str) -> dict:
    """
    ALWAYS call this tool when you are recommending a movie to the user.
    Pass the title of the movie to fetch its real poster URL, rating, and details.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/search/movie",
            params={
                "api_key": TMDB_API_KEY,
                "query": title, 
                "include_adult": "false", 
                "page": 1
            }
        )
        data = response.json()
        
        if data.get("results") and len(data["results"]) > 0:
            movie = data["results"][0]
            return {
                "id": str(movie["id"]),
                "title": movie["title"],
                "year": movie.get("release_date", "N/A")[:4],
                "genre": "Movie", 
                "rating": str(round(movie.get("vote_average", 0), 1)),
                "tags": [], 
                "image": f"{IMAGE_BASE_URL}{movie['poster_path']}" if movie.get("poster_path") else ""
            }
        return {"error": f"Could not find details for {title}"}