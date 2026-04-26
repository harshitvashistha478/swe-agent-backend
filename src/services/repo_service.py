import os
import subprocess
from urllib.parse import urlparse


def extract_repo_name(repo_url: str) -> str:
    path = urlparse(repo_url).path
    return path.strip("/").replace(".git", "")


def build_clone_url(repo_url: str, token: str | None):
    if token:
        # inject token into URL for private repo
        return repo_url.replace("https://", f"https://{token}@")
    return repo_url


def clone_repo(repo_url: str, visibility: str, access_token: str | None):
    if visibility == "private" and not access_token:
        raise ValueError("Access token required for private repo")

    repo_name = extract_repo_name(repo_url)
    clone_url = build_clone_url(repo_url, access_token)

    clone_path = os.path.join("repos", repo_name)

    os.makedirs("repos", exist_ok=True)

    if os.path.exists(clone_path):
        raise ValueError("Repository already exists")

    try:
        subprocess.run(
            ["git", "clone", clone_url, clone_path],
            check=True
        )
    except subprocess.CalledProcessError:
        raise RuntimeError("Failed to clone repository")

    return repo_name