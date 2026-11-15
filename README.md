# Print Queue

Local web page for lining up 3D prints while the printer is busy.

```
cd printqueue
python3 app.py
```

Open http://localhost:8765 (the terminal also prints a link for your phone on the same Wi-Fi).

- Drop STL / 3MF / OBJ / G-code / STEP files anywhere on the page, or add an idea with notes.
- STLs get a rotatable preview plus size (mm) and triangle count.
- Drag ⋮⋮ to reorder, ▶ Print now marks the current job, ✓ Finished moves it to Done.
- Click a title or notes to edit them inline.

Everything is stored in `data/` (`queue.json` + `uploads/`). Removed items' files are moved to `data/removed/`, not deleted.
No dependencies — standard-library Python 3.8+. Use `--port` to change the port, `--host 127.0.0.1` to keep it off the network.
