# How to Test StackSearch

Hi! This guide is for testing the app. You don't need to run any commands — everything is already built. Just follow the steps below.

## What this app does

StackSearch is a search box for your own files. You point it at a folder, it reads your files, and then you can search them by keyword or ask questions in plain English. Everything stays on your computer — nothing is uploaded.

## Before you start

I already prepared a test folder for you with sample files. It is here:

```
~/Documents/StackSearchTest/
```

It contains notes, an invoice, a Python file, a chat log, an email, and a PDF. You will search these.

## Step 1 — Open the app

1. Open **StackSearch** from your Applications folder (or the Dock).
2. A small search bar window appears (it floats on top of other windows).
3. If nothing appears, press the keyboard shortcut: **Cmd + Shift + Space**.

> If the shortcut does nothing, another app may be using it. Open Settings (top right) → Shortcut, and pick a different one (for example `Cmd + Shift + J`), then click **bind**.

## Step 2 — Add your test folder as a "source"

A "source" is just a folder the app should read.

1. Click **settings** (top right of the window).
2. In the **sources** section, set:
   - **kind**: `files`
   - **name**: `Test Files`
   - **config**: paste this exactly:
     ```json
     { "paths": ["~/Documents/StackSearchTest"], "exclude": [".git", "node_modules"] }
     ```
3. Click **add source**.

### What is "reindexing"?

When you add a folder, the app scans every file in it and reads them into its search index. This is called **indexing**. You will see a number like "5 jobs queued" at the top while it works. When it reaches 0, you are ready to search. You do not need to click anything else — it happens automatically.

If you ever change a file and want the app to re-read it, click the **reindex** button next to that source. That forces a fresh scan.

## Step 3 — Search (keyword)

Go back to the **search** tab (top left). Type these one at a time and press Enter:

| Type this | You should see |
|---|---|
| `November 3` | The internship note and the offer email |
| `order ID 48213` | The invoice file |
| `milk` | The shopping list |
| `greet` | The Python file (hello.py) |
| `Rahul` | The WhatsApp chat |
| `roadmap` | The PDF (project roadmap) |
| `password` | Nothing — the secret was scrubbed for safety |

Click any result to open the original file.

## Step 4 — Ask a question (AI answer)

The app can answer questions in plain English and shows which file it got the answer from.

Try these:

| Ask this | Expected answer (roughly) |
|---|---|
| `When do I start my internship?` | "November 3, 2026" with a citation like [1] |
| `What is the invoice amount?` | "$129.99" |
| `Who is my mentor?` | "Sarah Chen" |
| `What is the capital of France?` | "I couldn't find that in your data." (it refuses to guess) |

Hover over a citation number (like [1]) to see the exact passage. Click it to open the file.

## Step 5 — Try filters

- After a search, use the filter chips (source, date) to narrow results.
- Keyboard: **Arrow Up/Down** to move through results, **Enter** to open, **Tab** to switch between results and the answer panel.

## Step 6 — Settings you can play with

- **Answer model**: switch between "Qwen3-4B (default)" and "Qwen3-8B (quality)". The bigger one is slower but slightly more accurate. Switching unloads the model; the next question reloads it.
- **Shortcut**: change the global hotkey.
- **Indexing**: shows how many documents/chunks are indexed.
- **Thermal & memory**: "simulate battery low" pauses background indexing (as if on battery). "set idle unload" changes how long the AI model stays loaded in memory.
- **Full Disk Access**: if you add real folders like Mail or Safari history, macOS may ask for permission. Click **open privacy settings** to grant it.

## Troubleshooting

- **"no results yet"**: make sure indexing finished (queued = 0 in Settings). If a source shows errors, check the path exists.
- **Permission denied**: see the note above about Full Disk Access, or re-add the source with a path inside your home folder.
- **App window won't appear**: press Cmd+Shift+Space, or click the menu-bar icon (top right of your screen) → "Show / Hide".
- **Secrets not found**: that's intentional — the app scrubs things like API keys and passwords before indexing, for your safety.

## What to report back

- Did each search return the expected file?
- Did the AI answer cite the right source?
- Any crashes, blank screens, or confusing messages?

Thank you for testing!
EOF
echo "written"