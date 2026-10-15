"""Check runtime URLs and early theme setup used by Material navigation."""

import gzip
from xml.etree import ElementTree

import pytest
from aiohttp.test_utils import TestClient, TestServer

from user_docs.service import UserDocsService


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False])
async def test_docs_sitemap_uses_served_origin_including_port(tmp_path, compressed):
    (tmp_path / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>http://localhost:18100/docs/</loc><lastmod>2026-09-08</lastmod></url>'
        '<url><loc>http://localhost:18100/docs/quickstart/</loc></url></urlset>'
    )
    service = UserDocsService(docs_root=tmp_path)
    async with TestClient(TestServer(service.build_app())) as client:
        suffix = ".gz" if compressed else ""
        response = await client.get("/docs/sitemap.xml" + suffix)
        body = await response.read()
        root = ElementTree.fromstring(gzip.decompress(body) if compressed else body)
        locations = [node.text for node in root.findall(".//{*}loc")]
        assert locations == [str(client.make_url(path)) for path in ("/docs/", "/docs/quickstart/")]
        assert response.headers["Cache-Control"] == "no-store"
        assert root.find("{*}url/{*}lastmod").text == "2026-09-08"


@pytest.mark.asyncio
async def test_docs_missing_sitemap_is_a_clear_404(tmp_path):
    async with TestClient(TestServer(UserDocsService(docs_root=tmp_path).build_app())) as client:
        response = await client.get("/docs/sitemap.xml")
        assert response.status == 404
