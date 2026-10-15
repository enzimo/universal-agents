"""
HTML fetching tool for Strands agents.
"""
import requests
from strands import tool


@tool
def fetch_html(url: str) -> str:
    """
    Retrieves raw HTML content from the given URL.

    Args:
        url: The URL of the webpage to retrieve.

    Returns:
        The HTML content of the webpage, or an error message if retrieval fails.
    """
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        return f"Error retrieving webpage: {e}"