"""Tests of the fork's workflow and vision tools (batch, save, publish, previews, capture helpers)."""

from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as McpImage
from mcp.types import TextContent
from multiconn_archicad import SoloProjectID, UntitledProjectID
from PIL import Image

from tapir_archicad_mcp.capture import capture_when_settled, crop_fraction, encode_image, looks_blank
from tapir_archicad_mcp.tools.custom.functions import archicad_list_commands
from tapir_archicad_mcp.tools.custom.vision_tools import (
    archicad_get_preview_image,
    archicad_publish,
)
from tapir_archicad_mcp.tools.custom.workflow_tools import (
    BatchStep,
    archicad_batch,
    archicad_get_guide,
    save_and_verify,
)

SUCCESS = {"success": True}


def _png_base64(size=(40, 20), color=(200, 30, 30)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture(autouse=True)
def capture_folder(tmp_path, monkeypatch):
    folder = tmp_path / "captures"
    monkeypatch.setenv("TAPIR_MCP_CAPTURE_DIR", str(folder))
    return folder


# --- image helpers -----------------------------------------------------------


def test_crop_fraction_cuts_the_requested_area():
    image = Image.new("RGB", (200, 100))
    assert crop_fraction(image, [0.5, 0.0, 1.0, 0.5]).size == (100, 50)
    assert crop_fraction(image, None) is image


@pytest.mark.parametrize("crop", [[0, 0, 1], [0.5, 0, 0.4, 1], [0, 0, 1.2, 1]])
def test_crop_fraction_rejects_bad_boxes(crop):
    with pytest.raises(ValueError):
        crop_fraction(Image.new("RGB", (10, 10)), crop)


def test_encode_image_scales_the_long_side_and_keeps_full_size_on_zero():
    image = Image.new("RGB", (4000, 1000), (10, 20, 30))
    data, size = encode_image(image, 2048, "jpeg")
    assert size == (2048, 512)
    assert Image.open(io.BytesIO(data)).format == "JPEG"
    _, full = encode_image(image, 0, "png")
    assert full == (4000, 1000)
    with pytest.raises(ValueError):
        encode_image(image, -1, "png")


def test_looks_blank_tells_uniform_from_drawn():
    image = Image.new("RGB", (100, 100), "white")
    assert looks_blank(image)
    image.paste((0, 0, 0), (10, 10, 60, 60))
    assert not looks_blank(image)


def test_capture_waits_until_two_grabs_match():
    frames = iter([Image.new("L", (10, 10), v) for v in (0, 120, 200, 200)])
    now = [0.0]
    image, settled = capture_when_settled(
        lambda: next(frames), timeout_seconds=10, clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert settled and image.getpixel((0, 0)) == 200


def test_capture_gives_up_at_the_timeout():
    values = iter(range(0, 250, 10))
    now = [0.0]
    _, settled = capture_when_settled(
        lambda: Image.new("L", (10, 10), next(values)), timeout_seconds=1, interval_seconds=0.5,
        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert not settled


# --- discovery ---------------------------------------------------------------


def test_list_commands_filters_by_every_query_word(fake_archicad):
    names = {c.name for c in archicad_list_commands("publisher set")}
    assert "navigator_publish_publisher_set" in names
    assert all("publisher" in n or "publish" in n for n in names)
    assert len(archicad_list_commands()) > len(names)


def test_guide_is_packaged():
    assert "archicad_capture_view" in archicad_get_guide()


# --- batch -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_runs_steps_in_order_and_reports_each(fake_archicad):
    fake_archicad.on_tapir_command("FitInWindow", SUCCESS)
    fake_archicad.on_tapir_command("GetCurrentWindowType", {"currentWindowType": "FloorPlan"})
    result = await archicad_batch(
        fake_archicad.port,
        [BatchStep(name="navigator_fit_in_window", params={}), BatchStep(name="app_get_current_window_type")],
        expect_window_type="FloorPlan",
    )
    assert result["succeeded"] == 2 and result["firstFailedIndex"] is None
    assert [c[0] for c in fake_archicad.calls] == ["GetCurrentWindowType", "FitInWindow", "GetCurrentWindowType"]


@pytest.mark.asyncio
async def test_batch_sends_nothing_when_a_step_is_invalid(fake_archicad):
    with pytest.raises(ToolError, match="Step 1"):
        await archicad_batch(fake_archicad.port, [
            BatchStep(name="navigator_fit_in_window", params={}), BatchStep(name="no_such_command"),
        ])
    assert fake_archicad.calls == []


@pytest.mark.asyncio
async def test_batch_stops_at_a_reported_failure(fake_archicad):
    fake_archicad.on_tapir_command("FitInWindow", {"success": False, "error": {"code": 1, "message": "nope"}})
    result = await archicad_batch(fake_archicad.port, [
        BatchStep(name="navigator_fit_in_window", params={}),
        BatchStep(name="app_get_current_window_type"),
    ])
    assert result["firstFailedIndex"] == 0 and result["notRun"] == 1
    assert "nope" in result["steps"][0]["error"]


@pytest.mark.asyncio
async def test_batch_refuses_the_wrong_window(fake_archicad):
    fake_archicad.on_tapir_command("GetCurrentWindowType", {"currentWindowType": "Layout"})
    with pytest.raises(ToolError, match="Layout"):
        await archicad_batch(fake_archicad.port, [BatchStep(name="navigator_fit_in_window", params={})],
                             expect_window_type="FloorPlan")
    assert [c[0] for c in fake_archicad.calls] == ["GetCurrentWindowType"]


# --- save --------------------------------------------------------------------


def test_save_reports_the_file_change(fake_archicad, tmp_path):
    project = tmp_path / "house.pln"
    project.write_bytes(b"old")
    old = time.time() - 100
    os.utime(project, (old, old))
    fake_archicad.archicad_id = SoloProjectID(projectName="house", projectPath=str(project))
    fake_archicad.on_tapir_command("SaveProject", SUCCESS)
    original = fake_archicad.core.post_tapir_command

    def save(command, parameters=None):
        if command == "SaveProject":
            project.write_bytes(b"new")
        return original(command, parameters)

    fake_archicad.core.post_tapir_command = save
    result = save_and_verify(fake_archicad.port, 2, "test")
    assert result["saved"] is True and result["modifiedAfter"] > result["modifiedBefore"]


def test_save_says_when_the_file_did_not_change(fake_archicad, tmp_path):
    project = tmp_path / "house.pln"
    project.write_bytes(b"old")
    fake_archicad.archicad_id = SoloProjectID(projectName="house", projectPath=str(project))
    fake_archicad.on_tapir_command("SaveProject", SUCCESS)
    result = save_and_verify(fake_archicad.port, 0, "test")
    assert result["saved"] is False and "note" in result


def test_save_refuses_an_untitled_project(fake_archicad):
    fake_archicad.archicad_id = UntitledProjectID()
    with pytest.raises(ToolError, match="untitled"):
        save_and_verify(fake_archicad.port, 0, "test")
    assert fake_archicad.calls == []


# --- previews and publishing --------------------------------------------------


@pytest.mark.asyncio
async def test_preview_image_comes_back_as_an_image(fake_archicad, capture_folder):
    fake_archicad.on_tapir_command("GetElementPreviewImage", {"previewImage": _png_base64()})
    blocks = await archicad_get_preview_image(
        fake_archicad.port, element_guid="11111111-2222-3333-4444-555555555555",
    )
    assert isinstance(blocks[0], TextContent) and isinstance(blocks[1], McpImage)
    assert len(list(capture_folder.glob("*.png"))) == 1


@pytest.mark.asyncio
async def test_preview_needs_exactly_one_target(fake_archicad):
    with pytest.raises(ToolError):
        await archicad_get_preview_image(fake_archicad.port)


@pytest.mark.asyncio
async def test_publish_lists_written_files_and_renders_pdf_pages(fake_archicad, tmp_path):
    out = tmp_path / "pdf"
    fake_archicad.on_tapir_command("PublishPublisherSet", SUCCESS)
    original = fake_archicad.core.post_tapir_command

    def publish(command, parameters=None):
        if command == "PublishPublisherSet":
            target = Path(parameters["outputPath"]) / "sheets.pdf"
            pages = [Image.new("RGB", (420, 297), c) for c in ("white", "gray")]
            pages[0].save(target, "PDF", save_all=True, append_images=pages[1:])
        return original(command, parameters)

    fake_archicad.core.post_tapir_command = publish
    blocks = await archicad_publish(fake_archicad.port, "PDF", str(out), render_pages=5, wait_seconds=0)
    images = [b for b in blocks if isinstance(b, McpImage)]
    assert len(images) == 2
    assert (out / "sheets_p1.png").exists() and (out / "sheets_p2.png").exists()
