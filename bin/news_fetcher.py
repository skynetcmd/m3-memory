import asyncio
import requests
import json
from datetime import datetime
import os
import logging
import sys
from typing import List, Dict, Any

# Assuming MCP is a custom library in your system
try:
    from mcp import FastMCP
except ImportError:
    class FastMCP:
        def __init__(self, name):
            self.name = name
        
        def tool(self):
            def decorator(func):
                return func
            return decorator

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news_fetcher")

# Initialize MCP
mcp = FastMCP("News Fetcher")

# API configuration - using NewsAPI.org which provides free news headlines
NEWS_API_URL = "https://newsapi.org/v2/top-headlines"
API_KEY = os.getenv("NEWS_API_KEY", "YOUR_API_KEY_HERE")  # Replace with your own key or set in environment

def fetch_headlines(country: str = "us", category: str = "general", limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fetch top news headlines from NewsAPI.
    
    Args:
        country (str): Country code for news (default: 'us')
        category (str): News category (default: 'general')
        limit (int): Maximum number of headlines to return (default: 10)
    
    Returns:
        List of dictionaries containing news articles
    """
    try:
        params = {
            "country": country,
            "category": category,
            "apiKey": API_KEY,
            "pageSize": limit
        }
        
        response = requests.get(NEWS_API_URL, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        if data.get("status") == "ok":
            return data.get("articles", [])
        else:
            logger.error(f"API Error: {data.get('message', 'Unknown error')}")
            return []
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching news: {type(e).__name__}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding response: {type(e).__name__}")
        return []

def format_headlines(articles: List[Dict[str, Any]]) -> str:
    """
    Format the fetched news headlines into a string.
    
    Args:
        articles: List of dictionaries containing news articles
    
    Returns:
        Formatted string of headlines
    """
    if not articles:
        return "No news headlines found or there was an error fetching the news."
        
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output = f"\n=== Today's Top Headlines (Fetched at: {current_time}) ===\n\n"
    for i, article in enumerate(articles, 1):
        title = article.get("title", "No title available")
        source = article.get("source", {}).get("name", "Unknown source")
        published_at = article.get("publishedAt", "Unknown date")
        try:
            date_str = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            date_str = "Invalid date"
            
        description = article.get("description", "No description available")
        url = article.get("url", "#")
        
        output += f"{i}. {title}\n"
        output += f"   Source: {source} | Published: {date_str}\n"
        output += f"   Summary: {description}\n"
        output += f"   Read more: {url}\n\n"
    
    return output

def display_headlines(articles: List[Dict[str, Any]]) -> None:
    """
    Display the fetched news headlines in a formatted way.
    
    Args:
        articles: List of dictionaries containing news articles
    """
    print(format_headlines(articles))

@mcp.tool()
async def get_news_headlines(country: str = "us", category: str = "general", limit: int = 10) -> str:
    """
    MCP tool to fetch and return formatted news headlines.
    
    Args:
        country (str): Country code for news (default: 'us')
        category (str): News category (default: 'general')
        limit (int): Maximum number of headlines to return (default: 10)
    
    Returns:
        Formatted string of news headlines
    """
    logger.info(f"Fetching headlines for country: {country}, category: {category}")
    news_articles = await asyncio.to_thread(fetch_headlines, country=country, category=category, limit=limit)
    return format_headlines(news_articles)

if __name__ == "__main__":
    # Check if API key is the default placeholder
    if API_KEY == "YOUR_API_KEY_HERE":
        print("WARNING: You need to set your NewsAPI key.")
        print("To get a NewsAPI key, follow these steps:")
        print("1. Visit https://newsapi.org/")
        print("2. Click on 'Get API Key' or 'Register' to create a free account.")
        print("3. Fill in the required information (name, email, etc.).")
        print("4. After signing up, you will receive an API key.")
        print("5. Set it in your environment with: export NEWS_API_KEY='your_key'")
        print("   (On Windows, use: set NEWS_API_KEY=your_key)")
        print("Alternatively, you can replace 'YOUR_API_KEY_HERE' in this script with your actual key.")
        print("For now, the script will attempt to run with the placeholder key, but it will likely fail.\n")
    
    # Allow command line arguments for country and category
    country_code = "us"
    news_category = "general"
    
    if len(sys.argv) > 1:
        country_code = sys.argv[1]
    if len(sys.argv) > 2:
        news_category = sys.argv[2]
        
    print(f"Fetching headlines for country: {country_code}, category: {news_category}")
    news_articles = fetch_headlines(country=country_code, category=news_category)
    display_headlines(news_articles)
