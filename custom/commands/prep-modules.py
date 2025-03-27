import logging
import json
import os
import time
from ruamel.yaml.comments import CommentedMap as OrderedDict
import ruamel.yaml
from pathlib import Path

import click
import git
import requests
from spacemk import load_normalized_data
from requests_toolbelt.utils import dump as request_dump
import shutil

def get_latest_tag(endpoint: str, github_api_token: str, namespace: str, repository: str) -> dict:
    data = {}

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_api_token}",
    }

    try:
        url = f"{endpoint}/repos/{namespace}/{repository}/tags?per_page=5"
        response = requests.get(headers=headers, url=url)
        logging.debug(request_dump.dump_all(response).decode("utf-8"))
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"HTTP Error: {e}") from e

    tags = response.json()
    if not tags:
        return None
    
    # Get the first tag (most recent)
    tag = tags[0]
    tag_name = tag.get("name").removeprefix("v")
    data[tag_name] = tag["commit"]["sha"]
    return data

def checkout_repository(endpoint: str, github_api_token: str, namespace: str, repository: str, tag: str, modules_dir: str) -> str:
    """
    Checkout a GitHub repository using GitPython.
    If the repository is already cloned, fetch updates instead.
    
    Args:
        endpoint (str): GitHub API endpoint
        github_api_token (str): GitHub API token
        namespace (str): Repository namespace/owner
        repository (str): Repository name
        tag (str): Tag to checkout
        modules_dir (str): Directory to store modules
        
    Returns:
        str: Path to the cloned/updated repository
    """
    # Create a temporary directory for the repository
    repo_dir = Path(modules_dir) / repository
    
    # Construct the repository URL with authentication
    if endpoint == "https://api.github.com":
        # Public GitHub
        repo_url = f"https://{github_api_token}@github.com/{namespace}/{repository}.git"
    else:
        # GitHub Enterprise
        # Extract the domain from the API endpoint (e.g., "https://api.github.example.com" -> "github.example.com")
        domain = endpoint.replace("https://api.", "")
        repo_url = f"https://{github_api_token}@{domain}/{namespace}/{repository}.git"
    
    try:
        if repo_dir.exists() and (repo_dir / ".git").exists():
            # Repository already exists, update it
            logging.info(f"Repository {namespace}/{repository} already exists at {repo_dir}, fetching updates")
            repo = git.Repo(repo_dir)
            
            # Add remote if it doesn't exist, or update existing origin
            try:
                origin = repo.remote("origin")
                if origin.url != repo_url:
                    origin.set_url(repo_url)
            except ValueError:
                origin = repo.create_remote("origin", repo_url)
            
            # Fetch latest changes
            origin.fetch()
            repo.git.checkout()
        else:
            # Clone the repository
            logging.info(f"Cloning repository {namespace}/{repository} to {repo_dir}")
            repo = git.Repo.clone_from(repo_url, repo_dir)
            repo.git.checkout()
        
        return str(repo_dir)
    except git.GitCommandError as e:
        logging.error(f"Git error: {e}")
        raise RuntimeError(f"Failed to clone/update repository: {e}") from e

def create_pull_request(owner: str, repository: str, title: str, description: str, head_branch: str, base_branch: str, github_api_token: str):
    """Creates the pull request for the head_branch against the base_branch"""
    
    logging.info(f"Creating pull request for '{head_branch}' against '{base_branch}'")
    git_pulls_api = "https://api.github.com/repos/{0}/{1}/pulls".format(
        owner,
        repository)
    headers = {
        "Authorization": "token {0}".format(github_api_token),
        "Content-Type": "application/json"}

    payload = {
        "title": title,
        "body": description,
        "head": head_branch,
        "base": base_branch,
    }

    r = requests.post(
        git_pulls_api,
        headers=headers,
        data=json.dumps(payload)
        )

    if not r.ok:
        print("Request Failed: {0}".format(r.text))

def update_spacelift_config(module_name: str, latest_tag_data: dict, repo_dir: str = None):
    """
    Check if .spacelift/config.yml exists and update the module_version element.
    If the file doesn't exist, create it.
    
    Args:
        module_name (str): The name of the module
        latest_tag_data (dict): Dictionary containing the latest tag information
        repo_dir (str, optional): Path to the repository directory
        
    Returns:
        bool: True if changes were made, False otherwise
    """
    if not latest_tag_data:
        logging.warning(f"No tags found for module '{module_name}'. Skipping config update.")
        return False
        
    changes_made = False
    
    yaml = ruamel.yaml.YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    # Get the latest tag name (first key in the dictionary)
    latest_tag_version = next(iter(latest_tag_data))

    config_dir = Path(repo_dir) / ".spacelift"
    config_file = config_dir / "config.yml"

    # Create .spacelift directory if it doesn't exist
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)
        logging.info(f"Created directory '{config_dir}'")

    # Create .spacelift directory if it doesn't exist
    if not config_dir.exists():
        os.makedirs(config_dir)
        logging.info(f"Created directory '{config_dir}'")

    # Check if config.yml exists
    if config_file.exists():
        # Read existing config
        try:
            with open(config_file, 'r') as file:
                config_data = file.read()
                config = yaml.load(config_data)
        except Exception as e:
            logging.error(f"Error reading {config_file}: {e}")
            config = OrderedDict()
    else:
        # Create new config
        config = OrderedDict()
        config['version'] = "1"
        logging.info(f"Creating new config file '{config_file}'")

    # Update module_version
    config['module_version'] = latest_tag_version
    
    # Write updated config
    try:
        # Check if file exists and if content would change
        if not config_file.exists() or config.get('module_version') != latest_tag_version:
            with open(config_file, 'w') as file:
                yaml.dump(config, file)
            logging.info(f"Updated module_version to '{latest_tag_version}' in '{config_file}'")
            changes_made = True
    except Exception as e:
        logging.error(f"Error writing to {config_file}: {e}")

    github_worfklow_dir = Path(repo_dir) / ".github/workflows"
    tf_workflow_file = github_worfklow_dir / "release-please-tf.yaml"
    orig_workflow_file = github_worfklow_dir / "release-please.yaml"
    
    if orig_workflow_file.exists():
        os.remove(orig_workflow_file)
        logging.info(f"Removed original release-please workflow file '{orig_workflow_file}'")
        changes_made = True
        
    os.makedirs(github_worfklow_dir, exist_ok=True)

    # Copy release-please workflow template to the repository
    template_file = Path(os.path.dirname(__file__), '../misc/release-please-tf.yaml').resolve()
    if template_file.exists():
        if not tf_workflow_file.exists() or not os.path.samefile(template_file, tf_workflow_file):
            shutil.copy2(template_file, tf_workflow_file)
            logging.info(f"Copied release-please workflow template to '{tf_workflow_file}'")
            changes_made = True
    else:
        logging.warning(f"Template file '{template_file}' not found. Skipping workflow setup.")
        
    return changes_made

def commit_changes(repo_dir: str, module_name: str, latest_tag_version: str, namespace: str, repository: str, github_api_token: str) -> bool :
    """
    Commit changes made to the module repository.
    
    Args:
        repo_dir (str): Path to the repository directory
        module_name (str): The name of the module
        latest_tag_version (str): The latest tag version
    
    Returns:
        bool: True if commit was successful, False otherwise
    """
    try:
        branch_name = "spacelift-migration"
        repo = git.Repo(repo_dir)
        
        current_branch = repo.active_branch
        
        # Check if there are changes to commit
        if not repo.is_dirty(untracked_files=True):
            logging.info(f"No changes to commit for module '{module_name}'")
            return False
        
        branch = repo.create_head(branch_name)
        branch.checkout()
        
        # Add all changes
        repo.git.add(all=True)
        
        # Commit changes
        commit_message = f"chore: update Spacelift configuration for version {latest_tag_version}"
        repo.git.commit('-m', commit_message)
        repo.git.push("origin", '-u', branch_name)
        time.sleep(1)
        
        create_pull_request(owner=namespace, 
                            repository=repository,
                            title= f"chore: update Spacelift configuration",
                            description= f"Create spacelift config file and update release-please workflow",
                            head_branch=branch_name,
                            base_branch=current_branch.name, 
                            github_api_token=github_api_token)
        
        logging.info(f"Committed changes to module '{module_name}' with message: '{commit_message}'")
        return True
    except git.GitCommandError as e:
        logging.error(f"Git error while committing changes: {e}")
        return False
    except Exception as e:
        logging.error(f"Error committing changes: {e}")
        return False

@click.command(help="Prepare modules.")
@click.decorators.pass_meta_key("config")
def prepare_modules(config):
    data = load_normalized_data()

    current_file_path = Path(__file__).parent.resolve()
    modules_path = Path(current_file_path, f"../../data/modules").resolve()
    os.makedirs(modules_path, exist_ok=True)

    for module in data.get("modules"):
        if module.get("vcs.repository") is None:
            logging.warning(f"Module '{module.get('name')}' has no repository information. Skipping")
            continue

        latest_tag_data = get_latest_tag(
            endpoint=config.get("github.endpoint", "https://api.github.com"),
            github_api_token=config.get("github.api_token"),
            namespace=module.get("vcs.namespace"),
            repository=module.get("vcs.repository"),
        )
        
        if not latest_tag_data:
            logging.warning(f"No tags found for module '{module.get('name')}'. Skipping")
            continue
            
        # Get the latest tag name (first key in the dictionary)
        latest_tag_version = next(iter(latest_tag_data))
        
        # Checkout the repository
        repo_dir = checkout_repository(
            endpoint=config.get("github.endpoint", "https://api.github.com"),
            github_api_token=config.get("github.api_token"),
            namespace=module.get("vcs.namespace"),
            repository=module.get("vcs.repository"),
            tag=f"{latest_tag_version}",
            modules_dir=modules_path
        )
        
        # Update .spacelift/config.yml with the latest tag
        changes_made = update_spacelift_config(module.get("name"), latest_tag_data, repo_dir)
        
        # Commit changes if any were made
        if changes_made:
            commit_success = commit_changes(repo_dir, module.get("name"), 
                                            latest_tag_version, 
                                            namespace=module.get("vcs.namespace"), 
                                            repository=module.get("vcs.repository"), 
                                            github_api_token=config.get("github.api_token"))
            if commit_success:
                logging.info(f"Successfully committed changes to module '{module.get('name')}'")
        
                                
        logging.info(f"Successfully prepared module '{module.get('name')}'")
