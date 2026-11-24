"""Fetch source documents only through an explicit local job."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from autopcb.jobs import _new_directory, write_json


def datasheets(proposal: dict, output: Path) -> dict:
    sources = {part["source"] for part in proposal.get("components", {}).values()}
    if not sources or len(sources) > 24:
        raise ValueError("Supply a proposal with between one and 24 source documents")
    files = []
    with _new_directory(output) as stage:
        for index, source in enumerate(sorted(sources), 1):
            endpoint = urlsplit(source)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
            ):
                raise ValueError(f"An HTTPS source URL is required: {source}")
            request = Request(
                source,
                headers={
                    "User-Agent": "AutoPCB/1.0",
                    "Accept": "application/pdf,text/html",
                },
            )
            # The request contains a public source URL only. No board data or
            # requirements are attached to the request.
            with urlopen(request, timeout=20) as response:
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("The maker's source redirected to a non-HTTPS URL")
                limit = 25 * 1024**2
                content = response.read(limit + 1)
                if not content or len(content) > limit:
                    raise ValueError(
                        f"Source is empty or exceeds the download limit: {source}"
                    )
            suffix = (
                ".pdf"
                if content.startswith(b"%PDF-")
                else ".html"
                if content.lstrip().startswith(b"<")
                else ".bin"
            )
            name = f"source-{index:02}{suffix}"
            (stage / name).write_bytes(content)
            files.append(
                {
                    "file": name,
                    "source": source,
                    "final_url": response.url,
                    "sha256": sha256(content).hexdigest(),
                }
            )
        result = {
            "schema_version": 1,
            "kind": "source_lookup",
            "status": "downloaded",
            "proposal_id": proposal.get("proposal_id"),
            "sources": files,
        }
        write_json(stage / "sources.json", result)
    return result
