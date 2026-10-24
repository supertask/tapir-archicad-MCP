"""Tools that let the model see Archicad: view images, preview images, published PDFs (fork addition).

Images go back as MCP image content the model can look at. The returned copy is
scaled to max_size on its long side (default 2048 px, readable and fine for a
slide); a full-resolution PNG is always kept on disk and its path returned.
"""

import asyncio
import base64
import io
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as McpImage
from mcp.types import TextContent, ToolAnnotations
from multiconn_archicad.errors import ArchicadAPIError
from PIL import Image

from tapir_archicad_mcp.app import mcp
from tapir_archicad_mcp.capture import (
    DEFAULT_MAX_SIZE,
    capture_dir,
    crop_fraction,
    encode_image,
    looks_blank,
    timestamped_path,
)
from tapir_archicad_mcp.context import command_guard_instance
from tapir_archicad_mcp.tools.custom.functions import _get_header
from tapir_archicad_mcp.tools.custom.workflow_tools import require_success, run_command

log = logging.getLogger(__name__)

ImageFormat = Literal["jpeg", "png"]
MAX_PDF_PAGES = 20
# Tapir 1.7.2+. Called by name so it works whatever the generated tool list contains;
# the tool name is what the command guard uses for its time limit and pending records.
VIEW_IMAGE_COMMAND = "SaveCurrentWindowAsImage"
VIEW_IMAGE_TOOL = "project_save_current_window_as_image"


def _image_block(image: Image.Image, max_size: int, image_format: ImageFormat) -> tuple[McpImage, tuple[int, int]]:
    try:
        data, size = encode_image(image, max_size, image_format)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return McpImage(data=data, format=image_format), size


def _text_block(info: dict) -> TextContent:
    return TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=2))


def _save_full(image: Image.Image, stem: str) -> str:
    path = timestamped_path(capture_dir(), stem, ".png")
    image.save(path, format="PNG")
    return str(path)


def export_view(
    port: int,
    navigator_item_guid: str | None,
    window: dict | None,
    fit_in_window: bool,
    fit_element_guids: list[str] | None,
    entire_drawing: bool,
) -> tuple[Image.Image, dict]:
    steps: list[str] = []
    if navigator_item_guid and window:
        raise ToolError("Give either navigator_item_guid or window, not both.")
    if navigator_item_guid:
        require_success("app_change_window", run_command(
            "app_change_window", port, {"navigatorItemId": {"guid": navigator_item_guid}}))
        steps.append(f"opened navigator item {navigator_item_guid}")
    elif window:
        require_success("app_change_window", run_command("app_change_window", port, window))
        steps.append(f"changed window to {window}")
    if fit_element_guids:
        elements = [{"elementId": {"guid": guid}} for guid in fit_element_guids]
        require_success("navigator_fit_in_window", run_command("navigator_fit_in_window", port, {"elements": elements}))
        steps.append(f"zoomed to {len(elements)} element(s)")
    elif fit_in_window:
        require_success("navigator_fit_in_window", run_command("navigator_fit_in_window", port, {}))
        steps.append("fit everything in the window")
    path = timestamped_path(capture_dir(), f"port{port}_view", ".png")
    params: dict[str, Any] = {"imageFilePath": str(path), "entireDrawing": entire_drawing}
    header = _get_header(port)
    guard = command_guard_instance.get()
    # Refuse over a dialog and bound the wait, the same way a command would.
    guard.preflight(header, port)
    try:
        result = guard.run(
            VIEW_IMAGE_TOOL, port, json.dumps(params, sort_keys=True),
            lambda: header.core.post_tapir_command(command=VIEW_IMAGE_COMMAND, parameters=params),
        )
    except ArchicadAPIError as e:
        raise ToolError(
            f"Archicad could not save the view as an image: {e}. {VIEW_IMAGE_COMMAND} needs Tapir 1.7.2 or later; "
            "check tapirVersion in discovery_list_active_archicads."
        ) from e
    if not path.is_file():
        raise ToolError(f"{VIEW_IMAGE_COMMAND} reported success but wrote no file at {path}.")
    with Image.open(path) as written:
        image = written.copy()
    info: dict[str, Any] = {"steps": steps, "fullResolutionPath": str(path)}
    for key in ("zoom", "rotation"):
        if isinstance(result, dict) and key in result:
            info[key] = result[key]
    return image, info


@mcp.tool(
    name="archicad_capture_view",
    title="Get the Current Archicad View as an Image",
    description=(
        "Returns a picture of what the current Archicad window shows (plan, section, 3D, layout...) so you can "
        "check your work visually. Archicad draws the image itself (Tapir SaveCurrentWindowAsImage, as File > "
        "Save As with a picture format), so it is right even when the window is behind others or not painted on "
        "screen; it holds the drawing area only, without menus or palettes. Optionally first opens a view "
        "(navigator_item_guid from the navigator tree, or window = the app_change_window params such as "
        "{'windowType': 'FloorPlan', 'storyIndex': 0} or {'windowType': '3DModel'}) and zooms (fit_in_window, or "
        "fit_element_guids to zoom onto elements). entire_drawing = the whole 2D drawing instead of the current "
        "zoom. crop = [left, top, right, bottom] as fractions of the image, to look closely at one area. The "
        "returned image is scaled to max_size px on its long side (default 2048; 0 = full resolution) as jpeg or "
        "png; the full-resolution PNG Archicad wrote is kept and its path returned (usable for slides). Needs "
        "Tapir 1.7.2+. The 3D camera cannot be set through the API on Archicad 26: use saved 3D views for fixed "
        "angles. Refused while a modal dialog is open. For a screenshot of the whole Archicad window or desktop, "
        "use an OS-level screenshot tool instead."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
async def archicad_capture_view(
    port: int,
    navigator_item_guid: str | None = None,
    window: dict | None = None,
    fit_in_window: bool = False,
    fit_element_guids: list[str] | None = None,
    entire_drawing: bool = False,
    crop: list[float] | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    image_format: ImageFormat = "jpeg",
) -> list[TextContent | McpImage]:
    image, info = await asyncio.to_thread(
        export_view, port, navigator_item_guid, window, fit_in_window, fit_element_guids, entire_drawing,
    )
    try:
        cropped = crop_fraction(image, crop)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if cropped is not image:
        info["croppedPath"] = await asyncio.to_thread(_save_full, cropped, f"port{port}_view_crop")
    block, size = _image_block(cropped, max_size, image_format)
    info.update({"fullResolution": list(image.size), "returnedSize": list(size)})
    if looks_blank(cropped):
        info["warning"] = "The image is almost uniform; the view may be empty here. Zoom or open another view."
    return [_text_block(info), block]


@mcp.tool(
    name="archicad_get_preview_image",
    title="Get an Element or Zone Preview Image",
    description=(
        "Returns Archicad's own rendered preview of one element (element_guid; image_type '3D', '2D' or "
        "'Section') or a plan clip around one zone (zone_guid; scale e.g. 0.01 for 1:100) as an image you can "
        "see, instead of base64 text. The image is also saved to disk and its path returned."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
async def archicad_get_preview_image(
    port: int,
    element_guid: str | None = None,
    zone_guid: str | None = None,
    image_type: Literal["3D", "2D", "Section"] = "3D",
    width: int = 1024,
    height: int = 1024,
    scale: float | None = None,
    max_size: int = DEFAULT_MAX_SIZE,
    image_format: ImageFormat = "jpeg",
) -> list[TextContent | McpImage]:
    if (element_guid is None) == (zone_guid is None):
        raise ToolError("Give exactly one of element_guid or zone_guid.")
    if element_guid:
        name, field = "elements_get_element_preview_image", "previewImage"
        params: dict[str, Any] = {"elementId": {"guid": element_guid}, "imageType": image_type,
                                  "format": "png", "width": width, "height": height}
    else:
        name, field = "elements_get_room_image", "roomImage"
        params = {"zoneId": {"guid": zone_guid}, "format": "png", "width": width, "height": height}
        if scale is not None:
            params["scale"] = scale
    result = require_success(name, await asyncio.to_thread(run_command, name, port, params))
    encoded = result.get(field) if isinstance(result, dict) else None
    if not encoded:
        raise ToolError(f"'{name}' returned no image: {result}")
    image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    image.load()
    full_path = await asyncio.to_thread(_save_full, image, f"port{port}_preview")
    block, size = _image_block(image, max_size, image_format)
    return [_text_block({"command": name, "fullResolutionPath": full_path, "returnedSize": list(size)}), block]


def _snapshot(folder: Path) -> dict[str, float]:
    if not folder.exists():
        return {}
    return {str(p): p.stat().st_mtime for p in folder.rglob("*") if p.is_file()}


def _render_pdf_pages(pdf_path: Path, page_limit: int, max_size: int) -> list[Image.Image]:
    import pypdfium2

    document = pypdfium2.PdfDocument(str(pdf_path))
    try:
        pages = []
        for index in range(min(len(document), page_limit)):
            page = document[index]
            width, height = page.get_size()  # points
            long_side = max(width, height) or 1
            target = max_size if max_size else 4096
            pages.append(page.render(scale=target / long_side).to_pil())
        return pages
    finally:
        document.close()


def publish_and_collect(
    port: int, publisher_set_name: str, output_folder: str, navigator_item_guids: list[str] | None, wait_seconds: float,
) -> dict:
    folder = Path(output_folder)
    if not folder.is_absolute():
        raise ToolError("output_folder must be an absolute path.")
    folder.mkdir(parents=True, exist_ok=True)
    before = _snapshot(folder)
    params: dict[str, Any] = {"publisherSetName": publisher_set_name, "outputPath": str(folder)}
    if navigator_item_guids:
        params["selectedNavigatorItemIds"] = [{"navigatorItemId": {"guid": g}} for g in navigator_item_guids]
    started = time.time()
    result = require_success("navigator_publish_publisher_set",
                             run_command("navigator_publish_publisher_set", port, params))
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        after = _snapshot(folder)
        written = sorted(p for p, m in after.items() if before.get(p) != m and m >= started - 1)
        if written or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return {"tapirResult": result, "outputFolder": str(folder), "writtenFiles": written}


@mcp.tool(
    name="archicad_publish",
    title="Publish a Publisher Set and Show the Pages",
    description=(
        "Publishes a publisher set (Tapir PublishPublisherSet) into output_folder, lists the files that were "
        "written, and returns the first pages of the written PDFs as images so you can check the sheets. "
        "Use it instead of printing through the GUI. Get the set names with navigator_get_publisher_set_names; "
        "publisher sets cannot be created through the API, so the project or template must already contain one "
        "set up for PDF. navigator_item_guids publishes only those items of the set. Rendered pages are saved as "
        "PNG next to the PDFs."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
)
async def archicad_publish(
    port: int,
    publisher_set_name: str,
    output_folder: str,
    navigator_item_guids: list[str] | None = None,
    render_pages: int = 3,
    max_size: int = DEFAULT_MAX_SIZE,
    image_format: ImageFormat = "jpeg",
    wait_seconds: float = 60.0,
) -> list[TextContent | McpImage]:
    info = await asyncio.to_thread(
        publish_and_collect, port, publisher_set_name, output_folder, navigator_item_guids, wait_seconds,
    )
    blocks: list[TextContent | McpImage] = []
    remaining = max(0, min(render_pages, MAX_PDF_PAGES))
    rendered: list[str] = []
    for file in info["writtenFiles"]:
        path = Path(file)
        if remaining <= 0 or path.suffix.lower() != ".pdf":
            continue
        pages = await asyncio.to_thread(_render_pdf_pages, path, remaining, 0)
        for number, page in enumerate(pages, start=1):
            png = path.with_name(f"{path.stem}_p{number}.png")
            await asyncio.to_thread(page.save, png, "PNG")
            rendered.append(str(png))
            block, _ = _image_block(page, max_size, image_format)
            blocks.append(block)
        remaining -= len(pages)
    info["renderedPages"] = rendered
    if not info["writtenFiles"]:
        info["warning"] = (
            "No new file appeared in the output folder. Check that the publisher set saves to files (not a "
            "printer or BIMcloud) and its format, or look for a dialog in Archicad (archicad_get_dialogs)."
        )
    return [_text_block(info), *blocks]
