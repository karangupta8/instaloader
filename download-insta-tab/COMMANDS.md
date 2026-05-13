# Instagram Downloader Command Cheat Sheet

This document lists various command combinations you can use to run the Instagram downloader tools (`carousel_processor.py` and `caption_graphic.py`).

Run these commands from the root directory of the project (`d:\ProjStuff\instaloader`).

---

## 🚀 1. Running the Server

Start the WebSocket server to listen for downloads from the browser console. Both `--sessionid` and `--csrftoken` are **required**.

### Arguments:
| Argument | Description |
| :--- | :--- |
| `--sessionid` | **Required**. Instagram sessionid cookie value |
| `--csrftoken` | **Required**. Instagram csrftoken cookie value |
| `--output` or `-o` | Root directory for downloads (default: current dir) |
| `--port` or `-p` | Port to listen on (default: 7432) |
| `--no-post-process`| Skip all post-processing (collage + caption graphic) |
| `--no-collage` | Skip carousel collage/concat |
| `--no-graphic` | Skip caption graphic snapshot |

### Examples:

**Basic with Required Keys:**
```powershell
python download-insta-tab/server.py --sessionid "YOUR_SESSION_ID" --csrftoken "YOUR_CSRF_TOKEN"
```

**Custom Output and No Post-Processing:**
```powershell
python download-insta-tab/server.py --sessionid "YOUR_SESSION_ID" --csrftoken "YOUR_CSRF_TOKEN" --output C:\Users\karan\Downloads --no-post-process
```

---

## 🎠 2. Carousel Processor (`carousel_processor.py`)

This script groups individual slide files (images/videos) into a composite grid or video.

| Use Case | Command |
| :--- | :--- |
| **Default** (Collage + Graphic + Delete Slides) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved` |
| **Keep Slides** (Don't delete originals) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --keep-slides` |
| **Move Slides** (Move originals instead of delete) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_slides_backup` |
| **No Graphic** (Don't append caption panel) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --no-graphic` |
| **No Collage** (Don't create the grid image) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --no-collage` |
| **Custom Size** (Larger cells for high-res) | `python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --cell-size 800` |

---

## 🖼️ 3. Caption Graphic (`caption_graphic.py`)

This script appends the caption and comments panel to media files.

| Use Case | Command |
| :--- | :--- |
| **Default** (Process new files only) | `python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved` |
| **Overwrite** (Re-generate existing snapshots) | `python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --overwrite` |
| **Delete Originals** (Delete media + txt after success) | `python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --delete` |
| **Move Originals** (Move instead of delete) | `python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_trash` |

---

## 💥 4. Combined "Power" Commands

Run both scripts in sequence to fully process a folder in one go.

### 🗑️ Mode A: Clean Up (Delete Originals)
Processes carousels, deletes slides, applies captions to everything, and deletes original source files.
```powershell
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved ; python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --delete
```

### 📁 Mode B: Archive (Move Originals)
Processes carousels, moves slides to backup, applies captions, and moves originals to trash.
```powershell
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_slides_backup ; python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_trash
```

### 💾 Mode C: Keep Everything
Processes carousels and applies captions but keeps all original files in the source folder.
```powershell
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --keep-slides ; python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved
```

### 🔄 Mode D: Force Re-process
Re-generates everything even if snapshots already exist.
```powershell
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved ; python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --overwrite
```

---

> [!TIP]
> In PowerShell, the `;` operator runs commands sequentially regardless of success. If you are using PowerShell 7+ or classic CMD and want the second command to run **only if the first succeeds**, use `&&` instead of `;`.
