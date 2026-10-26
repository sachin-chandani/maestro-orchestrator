# Capture from your phone: Apple Shortcuts and Android Tasker

Both send one HTTP request to your Maestro server's API, so they work with nothing else configured:
no Twilio, no Telegram. You need:

- Your server reachable from the phone over **HTTPS**, e.g. `https://maestro.example.com`, behind a reverse proxy with TLS. Don't expose plain HTTP: the API token would travel in clear text.
- `MAESTRO_API_TOKEN` set on the server. The phone sends it as `Authorization: Bearer <token>`.

The request every recipe below makes:

```bash
curl -X POST https://maestro.example.com/items \
  -H "Authorization: Bearer $MAESTRO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "call mom tomorrow at 5pm"}'
```

The answer is the saved item, including how it was understood:

```json
{"id": 12, "text": "Call mom", "kind": "reminder", "status": "pending",
 "due_at": "2026-10-28T17:00:00+05:30", "recurrence": null, "recurrence_label": null, ...}
```

`kind` is `note` and `due_at` is `null` when no time was found. A wrong or missing token gets `401`;
too many wrong tokens get `429` for a while.

## Apple Shortcuts (iPhone, iPad, Mac, Apple Watch)

Create a shortcut named **Remember** with these actions:

1. **Dictate Text** (or **Ask for Input** with input type *Text* if you prefer typing).
2. **Get Contents of URL**
   - URL: `https://maestro.example.com/items`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <your token>`, `Content-Type` = `application/json`
   - Request Body: `JSON`, one field: `text` (Text) = the *Dictated Text* variable
3. **Get Dictionary Value**: key `text` from *Contents of URL*.
4. **Get Dictionary Value**: key `due_at` from *Contents of URL*.
5. **Show Notification**: "Saved: *Dictionary Value* (*Dictionary Value 2*)".

Then: "Hey Siri, Remember" → speak → done. Add it to the Home Screen, the Action Button, or the share
sheet (set *Receive* to Text and use *Shortcut Input* instead of step 1) to capture selected text from
any app.

Tip: keep the token out of screenshots and shared shortcuts. Sharing a shortcut shares its headers.

## Android: Tasker

Create a task named **Remember**:

1. **Input → Get Voice** (or **Input Dialog** for typing). The result is in `%gv_heard` (or `%input`).
2. **Net → HTTP Request**
   - Method: `POST`
   - URL: `https://maestro.example.com/items`
   - Headers: `Authorization:Bearer <your token>` and `Content-Type:application/json` (one per line)
   - Body: `{"text": "%gv_heard"}`
3. **Alert → Flash**: `%http_data`. Shows the saved item; on errors, `%http_response_code` has the
   status.

Trigger it from a home-screen shortcut, a Quick Settings tile, or AutoVoice. If the text can contain
double quotes, escape them first (Variable Search Replace `"` → `\"`), or the JSON body breaks.

## Any other tool

Anything that can send an HTTPS POST with a header works the same way: HTTP Shortcuts (Android),
iOS Scriptable, Home Assistant `rest_command`, Raycast, Alfred, a cron job. See the interactive API docs
at `https://maestro.example.com/docs` for every field.
