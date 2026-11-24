"""research.py: fetch source documents only through an explicit local job.

The only data sent is the public source URL of each part in a reviewed proposal.

Copyright: GRANDE Robots and Aaron John Sabu
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from autopcb.jobs import NewDirectory, write_json


class SourceLookup:
    """Downloads the public source documents that a proposal's parts cite."""

    @staticmethod
    def _download(source: str) -> tuple[bytes, str]:
        """Fetch one source document over HTTPS.

        Inputs:
            source: Public URL of the document.
        Outputs:
            The document bytes and the final URL after redirects. Raises ValueError
            when the URL or its redirect is not HTTPS, carries credentials, or the
            document is empty or over 25 MiB.
        """
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
            return content, response.url

    @classmethod
    def run(cls, proposal: dict, output: Path) -> dict:
        """Download every distinct source document of a proposal into a new folder.

        Inputs:
            proposal: Design proposal whose components each name a ``source`` URL.
            output: Folder to create for the documents and ``sources.json``.
        Outputs:
            A source_lookup report with the file name, URL, final URL and SHA-256 of
            each document. Raises ValueError unless the proposal names between one
            and 24 distinct sources or a source is rejected, and OSError when a
            download fails. The folder is not created when anything fails.
        """
        sources = {part["source"] for part in proposal.get("components", {}).values()}
        if not sources or len(sources) > 24:
            raise ValueError(
                "Supply a proposal with between one and 24 source documents"
            )
        files = []
        with NewDirectory.stage(output) as stage:
            for index, source in enumerate(sorted(sources), 1):
                content, final_url = cls._download(source)
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
                        "final_url": final_url,
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
