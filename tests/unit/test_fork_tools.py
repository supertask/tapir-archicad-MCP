"""Tests of the fork's workflow and vision tools (batch, save, publish, previews, view images)."""

from __future__ import annotations

import base64
import io
import json
import os
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as McpImage
from mcp.types import TextContent
from multiconn_archicad import SoloProjectID, UntitledProjectID
from multiconn_archicad.errors import TapirCommandError
from PIL import Image

from tapir_archicad_mcp.capture import crop_fraction, encode_image, looks_blank
from tapir_archicad_mcp.tools.custom.functions import archicad_list_commands
from tapir_archicad_mcp.tools.custom.vision_tools import (
    archicad_capture_view,
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


def _archicad_writes_view_images(fake, response=None):
    """Make the fake answer SaveCurrentWindowAsImage by writing a drawn PNG, as Archicad does."""
    original = fake.core.post_tapir_command

    def post(command, parameters=None):
        if command != "SaveCurrentWindowAsImage":
            return original(command, parameters)
        fake.calls.append((command, parameters))
        image = Image.new("RGB", (300, 200), "white")
        image.paste((0, 0, 0), (50, 50, 250, 150))
        image.save(parameters["imageFilePath"], format="PNG")
        return {"imageFilePath": parameters["imageFilePath"], **(response or {})}

    fake.core.post_tapir_command = post


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
async def test_capture_view_returns_the_image_archicad_wrote(fake_archicad, capture_folder):
    fake_archicad.on_tapir_command("ChangeWindow", SUCCESS)
    fake_archicad.on_tapir_command("FitInWindow", SUCCESS)
    zoom = {"xMin": 0, "yMin": 0, "xMax": 10, "yMax": 5}
    _archicad_writes_view_images(fake_archicad, {"zoom": zoom, "rotation": 0})
    blocks = await archicad_capture_view(
        fake_archicad.port, window={"windowType": "FloorPlan", "storyIndex": 0}, fit_in_window=True,
        entire_drawing=True, max_size=150, image_format="png",
    )
    assert isinstance(blocks[0], TextContent) and isinstance(blocks[1], McpImage)
    command, params = fake_archicad.calls[-1]
    assert command == "SaveCurrentWindowAsImage" and params["entireDrawing"] is True
    assert Path(params["imageFilePath"]).parent == capture_folder
    info = json.loads(blocks[0].text)
    assert info["fullResolution"] == [300, 200] and info["returnedSize"] == [150, 100]
    assert info["zoom"] == zoom and len(info["steps"]) == 2 and "warning" not in info


@pytest.mark.asyncio
async def test_capture_view_crops_and_keeps_both_files(fake_archicad, capture_folder):
    _archicad_writes_view_images(fake_archicad)
    blocks = await archicad_capture_view(fake_archicad.port, crop=[0.0, 0.0, 0.5, 0.5], max_size=0)
    info = json.loads(blocks[0].text)
    assert info["returnedSize"] == [150, 100] and info["fullResolution"] == [300, 200]
    assert Path(info["croppedPath"]).is_file() and Path(info["fullResolutionPath"]).is_file()


@pytest.mark.asyncio
async def test_capture_view_explains_an_old_tapir(fake_archicad):
    def fail(command, parameters=None):
        raise TapirCommandError(message="Unknown command", code=1)

    fake_archicad.core.post_tapir_command = fail
    with pytest.raises(ToolError, match="Tapir 1.7.2"):
        await archicad_capture_view(fake_archicad.port)


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
