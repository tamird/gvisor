"""Keep tracked repository identities stable across verified GitHub transfers."""

REPOSITORY_ALIASES = {"gvisor/gvisor": "google/gvisor"}


def repository_identity(repo: str) -> str:
    return REPOSITORY_ALIASES.get(repo, repo)


def repository_url(url: str) -> str:
    for prefix in ("https://github.com/", "https://api.github.com/repos/"):
        if url.startswith(prefix):
            match url.removeprefix(prefix).split("/"):
                case [owner, name, *path]:
                    suffix = "/" + "/".join(path) if path else ""
                    return prefix + repository_identity(f"{owner}/{name}") + suffix
    return url


def api_repository_path(path: str) -> str:
    for location, identity in REPOSITORY_ALIASES.items():
        prefix = f"repos/{identity}/"
        if path.startswith(prefix):
            return f"repos/{location}/" + path.removeprefix(prefix)
    return path


def equivalent_repository_urls(url: str) -> tuple[str, ...]:
    stable = repository_url(url)
    urls = {stable}
    for alias, identity in REPOSITORY_ALIASES.items():
        prefix = f"https://github.com/{identity}/"
        if stable.startswith(prefix):
            urls.add(f"https://github.com/{alias}/" + stable.removeprefix(prefix))
    return tuple(sorted(urls))


def normalize_api_response(data: dict) -> dict:
    """Project identity fields into the registry namespace, preserving message bodies."""
    pending: list[dict | list] = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == "nameWithOwner" and isinstance(value, str):
                    item[key] = repository_identity(value)
                elif key in {"url", "html_url", "issue_url"} and isinstance(value, str):
                    item[key] = repository_url(value)
            children = item.values()
        else:
            children = item
        pending.extend(child for child in children if isinstance(child, (dict, list)))
    return data
