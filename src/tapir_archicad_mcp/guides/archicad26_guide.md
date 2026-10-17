# Working with Archicad 26 (Japanese edition) through this server

Read this once before building or editing a project. Every point here was learned on
Archicad 26 build 4007 JPN with the Tapir add-on, mostly the hard way.

## Tools in one line each

| Need | Tool |
| --- | --- |
| Find the running instances and their ports (always first) | `discovery_list_active_archicads` |
| Find a command | `archicad_list_commands` with `query` (e.g. `query="publisher"`) |
| Exact parameters of a command | `archicad_get_command_schema` |
| One command | `archicad_call_tool` |
| Many commands in a row | `archicad_batch` (one call, per-step results) |
| See the result | `archicad_capture_view` (window), `archicad_get_preview_image` (one element or zone) |
| PDF of sheets | `archicad_publish` (publisher set, then page images) |
| Save and be sure it saved | `archicad_save_project` |
| A dialog is blocking | `archicad_get_dialogs`, then ask the user to close it |
| A call timed out | `archicad_list_pending_commands` before resending anything |

## Working loop

1. `discovery_list_active_archicads`. Note which ports hold the user's real projects
   (`projectPath`); never write to them unless asked. With several instances, ask which one.
2. Read before writing: the command's schema, then the current state.
3. Write in batches (`archicad_batch`), small enough that a failure is easy to locate.
4. Check visually with `archicad_capture_view` (use `crop` to look closely; the full window
   is too small to judge dimensions or room names). Check sheets with `archicad_publish`.
5. Save with `archicad_save_project` after every heavy step.

## The active window decides what you read and edit

- Element commands work on the **active window's database**. A Layout window reads and edits
  drawings, not walls; the 3D window returns 0 walls. A count of 0 usually means the wrong
  window, not missing elements.
- Switch with `app_change_window` (`{"windowType": "FloorPlan", "storyIndex": 0}` or a
  navigator item) and check with `app_get_current_window_type`. `archicad_batch` can refuse to
  start in the wrong window (`expect_window_type`).
- A delete sent from the wrong window can report success and delete nothing: read back.

## Seeing the result

- `archicad_capture_view` paints the Archicad window into an image even when it is behind other
  windows. It refuses while a dialog is open and fails if the window is minimized.
- The returned image is 2048 px on its long side by default; a full-resolution PNG is saved and
  its path returned (use that file for slides).
- 2D zoom: `fit_in_window`, or `fit_element_guids` to zoom onto elements. A precise zoom box can
  only be stored in a view (`navigator_set_view_settings` with `saveZoom` and `zoom`) and applied
  by opening that view.
- The 3D camera cannot be set through the API on Archicad 26 with Tapir 1.7.1. For fixed angles,
  prepare saved 3D views in the View Map and open them by navigator item.

## Saving

- Use `archicad_save_project`; it confirms the file's modification time changed.
- Untitled projects cannot be saved by command: save once with
  `project_save_project_as_archive` (it becomes the open project) or ask the user for Save As.
- With Tapir 1.5.8, SaveProject failed whenever a non-plan window was active. Tapir 1.7.0+
  switches to the floor plan for the save; if it still fails, ask the user to press Ctrl+S.

## Text (Japanese)

- Tapir 1.5.8 `CreateTexts` stored text broken (`????`, or only the first letter) and such texts
  later crashed Archicad while drawings were redrawn (heap corruption). Tapir 1.7.0+ sends
  Unicode. After creating text, capture the view and check it really reads correctly.
- If broken texts exist in a project, delete them before re-creating or publishing drawings.
- There is no command to list fonts. Pick fonts through a favorite (`favoriteName`) or copy the
  settings of an existing text.

## Layouts and drawings

- Navigator inserts new items at the top: create folders and views in reverse order.
- Folder paths: never split view names on `/` (names contain scales like `1/100`).
- A drawing's position on create is the lower-left of its bounds; the bounds come from the
  view's saved zoom, so set a tight zoom on the view before creating drawings.
- `navigator_update_drawings` exists in Tapir 1.7.1; test it before relying on it. Re-creating
  drawings in bulk crashed Archicad 26 twice: save first and work in small groups.
- Elevations created from template markers came out empty for a new project. Build them as
  sections outside the building, facing in, or check the marker's range.

## PDF

- `archicad_publish` publishes a publisher set to a folder and shows the pages. Publisher sets
  cannot be created through the API: the project or template needs one set up for PDF.
- `project_print_view` has no output path and opens a dialog: avoid it.

## Dialogs, timeouts, crashes

- While a modal dialog is open Archicad processes nothing. Commands are refused with the
  dialog's title and buttons: ask the user to close it.
- A command abandoned after a time limit stays queued in Archicad and may run when the dialog
  closes. Never resend a write blindly: check `archicad_list_pending_commands` and read back.
- `Invalid program status (ongoing user input)`: a tool in the Archicad UI waits for input. Ask
  the user to press Esc.
- After a crash the reopening instance shows a project recovery dialog. Work done after the
  last save is only in the recovery copy.
- Each command is its own undo step; there is no API undo for a whole batch.
