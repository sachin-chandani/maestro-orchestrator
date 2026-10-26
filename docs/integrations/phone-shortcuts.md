# Capture from your phone: Apple Shortcuts and Android Tasker

Both send one HTTP request to your Maestro server's API, so they work with nothing else configured:
no Twilio, no Telegram. You need:

- Your server reachable from the phone over **HTTPS**, e.g. `https://maestro.example.com`, behind a reverse proxy with TLS. Don't expose plain HTTP: the API token would travel in clear text.
- `MAESTRO_API_TOKEN` set on the server. The phone sends it as `Authorization: Bearer <token>`.
- `MAESTRO_TRUST_PROXY_HEADERS=true` on the server when it's behind that proxy. Otherwise every request seems to come from the proxy, so anyone on the internet guessing tokens would get *your* phone locked out along with them.

The request every recipe below makes:

```bash
curl -X POST https://maestro.example.com/items \
  -H "Authorization: Bearer $MAESTRO_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "call mom tomorrow at 5pm"}'
```

The answer is the saved item, including how it was understood:

```json
{"id": 12, "text": "Call mom", "kind": "reminder", "status": "pending", "when": "Wed 28 Oct 17:00",
 "due_at": "2026-10-28T17:00:00+05:30", "recurrence": null, "recurrence_label": null}
```

`when` is ready to show a person; it's `note` when no time was found (and then `kind` is `note` and
`due_at` is `null`). A missing or wrong token gets `401`; after 10 wrong tokens in a minute, the client
gets `429` for a while.

## Apple Shortcuts (iPhone, iPad, Mac, Apple Watch)

Create a shortcut named **Remember** with these actions:

1. **Dictate Text** (or **Ask for Input** with input type *Text* if you prefer typing).
2. **Get Contents of URL**
   - URL: `https://maestro.example.com/items`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <your token>`, `Content-Type` = `application/json`
   - Request Body: `JSON`, one field: `text` (Text) = the *Dictated Text* variable
3. **Get Dictionary Value**: key `id` from *Contents of URL*.
4. **If** *Dictionary Value* **has any value** (only a saved item has an `id`; Shortcuts doesn't treat
   HTTP errors as failures, so check):
   - **Get Dictionary Value**: key `text`, then **Get Dictionary Value**: key `when`, both from
     *Contents of URL*.
   - **Show Notification**: "Saved: *text* (*when*)".
5. **Otherwise**:
   - **Get Dictionary Value**: key `detail` from *Contents of URL*.
   - **Show Notification**: "Not saved: *detail*" (e.g. "Invalid API token").
6. **End If**

Then: "Hey Siri, Remember" → speak → done. Add it to the Home Screen, the Action Button, or the share
sheet (set *Receive* to Text and use *Shortcut Input* instead of step 1) to capture selected text from
any app.

Tip: keep the token out of screenshots and shared shortcuts. Sharing a shortcut shares its headers.

## Android: Tasker

Create a task named **Remember**:

1. **Input → Get Voice**, then **Variables → Variable Set** `%text` to `%gv_heard`.
   (For typing instead: **Input → Input Dialog**, then **Variable Set** `%text` to `%input`.)
2. **Code → JavaScriptlet**: `var body = JSON.stringify({text: text});`
   This builds the request body safely whatever was said or typed (quotes, backslashes, new lines).
   Tasker passes `%text` into the script as `text` and gives the script's `body` back as `%body`.
3. **Net → HTTP Request**
   - Method: `POST`
   - URL: `https://maestro.example.com/items`
   - Headers: `Authorization:Bearer <your token>` and `Content-Type:application/json` (one per line)
   - Body: `%body`
4. **Alert → Flash**: if `%http_response_code` is `201`, show `Saved` and `%http_data`; otherwise show
   `Not saved: %http_data` (use **If** `%http_response_code ~ 201`).

Trigger it from a home-screen shortcut, a Quick Settings tile, or AutoVoice.

## Any other tool

Anything that can send an HTTPS POST with a header works the same way: HTTP Shortcuts (Android),
iOS Scriptable, Home Assistant `rest_command`, Raycast, Alfred, a cron job. See the interactive API docs
at `https://maestro.example.com/docs` for every field.
