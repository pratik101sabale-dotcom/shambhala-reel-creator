# Shambhala Reel Creator

A Windows-friendly tool for turning a local video into a vertical Reel. Choose a licensed music track, review the rendered MP4, pick or edit a caption with hashtags, and save a draft. Instagram publishing is optional and requires your own account, token, and confirmation.

## Quick start

1. Install Python and FFmpeg, then follow [Windows setup](#windows-setup).
2. Add your own music files and licence records as described in [Provide your files](#2-provide-your-files).
3. Open **Shambhala Reel Creator.bat** to choose a video and review a local preview.

Local rendering and drafts work without an Instagram account or token. To use the Instagram API, set your own numeric **Instagram professional account ID** (the Graph API ID, not your username) before opening the launcher or starting the MCP server:

```powershell
$env:IG_ACCOUNT_ID = Read-Host "Your Instagram professional account ID"
& ".\Shambhala Reel Creator.bat"
```

The launcher asks for a token at a hidden prompt only when you choose to publish. A draft belongs to the account selected when it was created, so create a fresh draft after setting or changing your account ID. Review the preview and caption before entering the confirmation phrase.

## Instagram MCP server

A local [MCP](https://modelcontextprotocol.io) server that uses stdio transport and the official MCP Python SDK. It has three read-only tools, three publishing tools and one upload test tool. Publishing is off by default and needs a person's confirmation every time.

| Read-only tool | What it does |
| --- | --- |
| `get_instagram_profile` | Sends one GET request to the Instagram Graph API and returns the account's `id`, `username`, `name`, `biography`, `website`, `profile_picture_url`, `followers_count`, `follows_count` and `media_count` as JSON. It takes no arguments. |
| `get_recent_instagram_posts` | Sends one GET request to the Instagram Graph API and returns the account's most recent posts as a JSON array. Each post has `id`, `caption`, `timestamp` and `permalink`, and `caption` is `null` if the post has none. The optional `limit` argument takes a whole number from 1 to 10 and defaults to 10. |
| `get_instagram_post_engagement` | Reads like and comment counts for the account's most recent posts and returns a JSON object with `posts` and `warnings`. Each post has `id`, `media_type`, `timestamp`, `permalink`, `like_count` and `comments_count`. The optional `limit` argument works the same way as above. |

| Publishing tool | What it does |
| --- | --- |
| `prepare_instagram_draft` | Validates a post or reel and saves it as a local draft. It doesn't use the network or need a token, and it can't publish. |
| `prepare_instagram_local_reel` | Validates a Reel in `reels/previews` and saves a local draft bound to the file's SHA-256 and size. It doesn't use the network or read the token. |
| `publish_instagram_draft` | Publishes one prepared draft, only after a person types its confirmation phrase into a dialog. For a local-file draft, the upload also waits for that phrase. |

| Test tool | What it does |
| --- | --- |
| `test_instagram_local_upload` | Sends one local-file draft to Meta as an unpublished test, after a person types its own phrase. It only runs when `IG_PUBLISHING_MODE` is `upload_test`, and has no way to publish. |

The server uses your `IG_ACCOUNT_ID` environment variable for Instagram requests. It has no code for messaging, or for editing or deleting posts.

## Publishing workflow

### 1. Prepare a draft

`prepare_instagram_draft` takes these arguments:

| Argument | Required | Notes |
| --- | --- | --- |
| `post_type` | Yes | `post` for one JPEG image, or `reel` for one MP4 or MOV video. |
| `media_url` | Yes | A public `https://` URL. Meta downloads the file from it, so it must stay online until publishing ends. |
| `caption` | No | Stored exactly as given. |
| `share_to_feed` | No | Reels only. It defaults to `true`, and `false` limits the reel to the Reels tab. |

It returns `valid`, `errors`, `warnings`, and `caption_stats`. A valid draft also has a `draft_id`, a `content_hash`, and a list of `unverified_requirements`. An invalid draft isn't saved.

A draft can't be edited. To change anything, prepare a new draft.

| What gets validated | Rule |
| --- | --- |
| Caption | At most 2,200 characters, 30 hashtags and 20 @mentions, with no control characters. Each emoji can count as 2 characters, which is the cautious reading of Meta's limit. |
| Media URL | Uses `https`, has a public host, and has only valid URL characters. It can't contain a username, password, `#fragment`, or access token. |
| File type in the URL | `.jpg` or `.jpeg` for a post, `.mp4` or `.mov` for a reel. Another media type is an error. If the URL doesn't show a type, you get a warning. |
| Secrets | The draft is refused if the caption or URL contains `IG_ACCESS_TOKEN`. |

**What this can't check:** the server doesn't download your media, so it can't confirm the file's real format, size, dimensions, duration or codecs. Check these yourself using `unverified_requirements`. Otherwise, Meta reports any problem when the container is created or processed, before anything is published.

| Media | Requirements from Meta's documentation |
| --- | --- |
| Image post | JPEG (not MPO or JPS), at most 8 MB, aspect ratio 4:5 to 1.91:1, width 320-1440 px (Meta scales other widths). |
| Reel | MP4 or MOV with the moov atom at the front and no edit lists. HEVC or H.264 video, progressive, closed GOP, 4:2:0, 23-60 FPS, VBR up to 25 Mbps, width up to 1920 px. AAC audio up to 48 kHz. 3 seconds to 15 minutes, at most 300 MB. 9:16 is recommended. |

### 2. Publish a draft

`publish_instagram_draft` takes the `draft_id` and `content_hash` from step 1. All of these must pass, or nothing is published:

| Safeguard | What it does |
| --- | --- |
| Publishing mode | `IG_PUBLISHING_MODE` must be set to `live` before the server starts. It is off when unset or set to anything else. |
| Exact draft | The `content_hash` must match, and the saved draft must still match its hash. An edited draft is refused. |
| A person's confirmation | Your MCP client shows the full draft in a dialog, and you type a phrase such as `PUBLISH C60B9A13`. The phrase comes from the draft's hash, so it only works for that draft. It is asked for on every attempt. |
| No confirmation, no publish | The phrase isn't a tool argument, so the AI assistant can't pass it in. If the client can't show the dialog, for example in a non-interactive session, the call fails. |
| One attempt at a time | A lock file stops two attempts from running on the same draft. |

No request reaches Meta until you confirm. The tool then follows [Meta's content publishing flow](https://developers.facebook.com/docs/instagram-platform/content-publishing/):

1. `GET /{account}/content_publishing_limit` checks the quota.
2. `POST /{account}/media` creates the container. This doesn't publish anything.
3. `GET /{container}?fields=status_code,status` checks processing, once a minute for up to 5 minutes as Meta recommends.
4. `POST /{account}/media_publish` publishes, only if the status is `FINISHED`.

The token needs `instagram_basic`, `instagram_content_publish` and `pages_read_engagement`. Meta requires `ads_management` or `ads_read` as well if your role on the Page comes through Business Manager.

### Draft status and what happens on failure

Drafts are saved in `drafts/`, which is in `.gitignore`. They contain no token.

| Status | Meaning | Calling publish again |
| --- | --- | --- |
| `prepared` | Nothing has been published. This includes a failed attempt to create the container. | Asks for confirmation again, then starts. |
| `container_created` | Meta holds a container that isn't published. It may still be processing, or Meta clearly refused to publish it. | Asks for confirmation again, then continues with the same container. |
| `publish_unknown` | A publish request was sent, but the result is unknown, for example after a timeout or server error. **The post may be live.** | Never publishes. Sends one status check, and records `published` if Meta confirms it. |
| `published` | The post is live. | Sends nothing and reports that it is already published. |
| `failed` | Meta reported the container as `ERROR` or `EXPIRED`. Containers expire after 24 hours. | Refuses. Prepare a new draft. |

**If a draft stays on `publish_unknown`:** check the account with `get_recent_instagram_posts`. Prepare a new draft only if the post isn't there.

**If a draft is blocked by a `.lock` file:** an attempt was interrupted, so the post could be live. Check the account first. Then, if no attempt is still running, delete `drafts/<draft_id>.lock`.

Meta's documentation disagrees on the daily limit, giving both 50 and 100 posts, so the server uses the limit the API reports. There is also a limit of 400 containers per 24 hours.

### Test safely

Go through these in order. The first three stages can't publish anything.

**Stage 1: mocked tests.** These need no token or network. See [Run the tests](#run-the-tests).

**Stage 2: prepare drafts.** With `IG_PUBLISHING_MODE` unset, ask your MCP client to prepare a draft. Then ask it to publish the draft, and check that the answer is `Publishing is off`.

**Stage 3: dry run.** This shows the real confirmation dialog without a token or any request to Meta. Start your MCP client from a PowerShell window where you've run:

```powershell
$env:IG_PUBLISHING_MODE = "dry_run"
```

Prepare a draft and ask to publish it. The dialog should start with `TEST ONLY — Nothing will be published to Instagram`. Then try declining the dialog, typing a wrong phrase, and typing the right phrase. Only the right phrase should return `dry_run_confirmed`. The dialog must come from your MCP client, and if you never see one, don't go on to stage 4.

**Stage 4: first live post.** This will publish for real, so only do it when you're ready. Use media you're happy to have public, and set `IG_PUBLISHING_MODE` to `live` only in the window you use for this test. Remove it afterward:

```powershell
Remove-Item Env:IG_PUBLISHING_MODE
```

If your client offers to always allow `publish_instagram_draft`, say no, so you keep its permission prompt as an extra check. Any automatic answer to the dialog, such as an `Elicitation` hook in Claude Code, would remove the human check, so don't set one up for this server.

## When engagement counts are unavailable

A count of `null` means Instagram didn't provide it, while `0` is a real count. The `warnings` list explains any `null` counts.

| Situation | What `get_instagram_post_engagement` does |
| --- | --- |
| Instagram leaves a count out of some posts, for example because like counts are hidden | Returns those counts as `null` with a warning. |
| Meta refuses the request because of a missing permission (error code 3, 10 or 200-299) or an invalid field (error code 100) | Sends one more GET request without the count fields, then returns the posts with both counts as `null` and Meta's explanation in `warnings`. |
| The second request is refused too | Returns an error. |
| Any other error, such as an expired token (code 190) or a rate limit (code 4) | Returns an error without retrying. |

If a permission is the problem, check that the token has `instagram_basic` and `pages_read_engagement`.

## Run the tests

The tests use mocked API responses, a fake token and a temporary drafts folder, so they don't contact Meta and don't need your real token. The publishing tests also make any real HTTP connection fail, and a fake client answers the confirmation dialog. They can't publish anything.

```powershell
$env:IG_ACCOUNT_ID = "1001"  # Synthetic account ID for the mocked tests
.\.venv\Scripts\python.exe -m unittest -v
Remove-Item Env:IG_ACCOUNT_ID
```

## Windows setup

Run these in PowerShell, from this folder. You only need to do this once.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.venv` is already in `.gitignore`.

## Provide the token at startup

The server reads the token from the `IG_ACCESS_TOKEN` environment variable. The steps below keep it in memory, in this one PowerShell window only.

**1. Enter the token at a hidden prompt.** Input is masked, and `Read-Host` input isn't saved to your PowerShell history.

```powershell
$secure = Read-Host "Instagram access token" -AsSecureString
$env:IG_ACCESS_TOKEN = [System.Net.NetworkCredential]::new("", $secure).Password
Remove-Variable secure
```

**2. Start the server.**

```powershell
.\.venv\Scripts\python.exe .\instagram_mcp_server.py
```

The server then waits silently for an MCP client on stdin. That is normal, and it won't print a prompt. Press `Ctrl+C` to stop it.

**3. Clear the token when you're done.** Closing the window also clears it.

```powershell
Remove-Item Env:IG_ACCESS_TOKEN
```

## Keeping the token safe

- Don't paste the token into Claude or any other chat.
- Don't type it directly in a command, such as `$env:IG_ACCESS_TOKEN = "..."`, because the line is saved to your PowerShell history.
- Don't use `setx`, because it stores the token unencrypted in your user registry settings, where it stays until you remove it.
- Don't put it in an MCP client config file, such as an `env` block. Those files are unencrypted.
- If the token is ever exposed, revoke it and create a new one in your Meta developer settings.

## Running it from an MCP client

Copy `.mcp.json.example` to `.mcp.json` and replace the example paths with your local paths to register the server for Claude Code. Set `IG_ACCOUNT_ID` before starting the client. The client starts the server itself, so the token and `IG_PUBLISHING_MODE` have to be in the environment the client inherits. Follow step 1 above, then launch the client from that same PowerShell window. Restart the client after changing either variable or updating the server, so that it picks up the change and the new tools.

Not every client passes its environment on to the servers it starts, and apps opened from the Start menu won't see a variable set in PowerShell. If the tool reports that `IG_ACCESS_TOKEN` is not set, check how your client handles environment variables.

## Errors

| Message | What to do |
| --- | --- |
| `IG_ACCESS_TOKEN is not set in the server's environment.` | Follow step 1, then start the server or client from that same window. |
| `Instagram Graph API returned HTTP 400` or `401` | The token is probably expired, invalid, or missing a permission. The message includes Meta's explanation. |
| `Could not reach the Instagram Graph API` | Check your internet connection and try again. |
| `The Instagram Graph API response had an unexpected format.` | Instagram answered, but not in the expected format. Try again, and check that the API version in `GRAPH_API_ROOT` is still supported. |
| `Publishing is off.` | This is the default. See [Test safely](#test-safely). |
| An error that mentions `elicitation` or the `form elicitation` capability | The client can't show the confirmation dialog, so nothing was published. Use an interactive session in a client that supports MCP elicitation. |
| `The confirmation phrase didn't match.` | Nothing was published. Call the tool again and type the phrase exactly as the dialog shows it. |
| `content_hash doesn't match this draft` or `changed after it was prepared` | The draft isn't the one you reviewed, so nothing was published. Prepare a new draft. |
| `The post may or may not be live.` | Don't prepare a new draft yet. Follow the steps for `publish_unknown` above. |
| HTTP error with subcode `2207052` | Meta couldn't fetch the media. Check that the URL is public and works without signing in. |
| HTTP error with subcode `2207027` | The media isn't ready yet. Wait, then call the tool again to continue with the same container. |
| HTTP error with subcode `2207042` | The account reached its publishing limit. Try again the next day. |
| `The upload failed` or `The upload couldn't finish` | Nothing was published. Check your connection and the message, then call the tool again to start a new upload. |
| `may not have access to resumable uploads` | Nothing was published. Check the token's type, permissions and assets in Meta's Access Token Debugger. |

## One-click launcher

Double-click **`Shambhala Reel Creator.bat`** in this folder for offline work. For publishing, start it from the PowerShell window where you set `IG_ACCOUNT_ID` as shown above. You don't need VS Code or a terminal open. The window stays open until you press a key, so you can read any error. The Desktop shortcut points to this folder, so moving the folder or the file breaks it.

| Step | What happens |
| --- | --- |
| 1. Choose a video | A file window opens. The original isn't changed. |
| 2. Choose music | Only tracks with a complete record in `reels/music/licenses.json` are offered, with the best match as the default. |
| 3. Review the preview | It renders a 1080x1920 MP4 in `reels/previews` and opens it. You can accept it, render again with new settings, or stop. Pressing Enter stops. |
| 4. Choose a caption | Pick one of three suggestions with hashtags, or write your own. Suggestions come from fixed templates, not an AI model, so check they fit the video. |
| 5. Decide about publishing | A draft is saved with publishing off. Pressing Enter stops here, with no token requested and nothing sent. |
| 6. Enter the token | Only after you answer `y`. It goes in at a hidden prompt, and right-click pastes it. |
| 7. Type the phrase | The server's own confirmation request appears. Check it, then type its phrase, or press Enter to decline. Nothing reaches Meta before this. |
| 8. Get the result | It shows the post ID and link. A failed attempt isn't retried. |

**What the launcher can't do:** it has no Instagram network code, and can't work out or supply the phrase. It starts `instagram_mcp_server.py` as an MCP client, so every server check still applies.

**Token and live mode:** the launcher ignores any `IG_ACCESS_TOKEN` or `IG_PUBLISHING_MODE` already set, and always asks in the run where you choose to publish. Both go only to the one server process for that attempt, and end with it. Neither is written to disk.

**If an attempt fails:** check Instagram before trying again. Every launcher run makes a new draft, so running it again after an uncertain result could post twice.

To test the setup without a token, a dialog or any Instagram request, run this in a PowerShell window in this folder:

```powershell
& ".\Shambhala Reel Creator.bat" --self-check
```

## Reel creator (local preview only)

`reel_creator.py` combines your video with music you have permission to use, and saves an Instagram-ready MP4 on your computer for you to review. It's separate from the MCP server: it has no network code, doesn't read your token or create drafts, and can't publish. Caption writing and handing a Reel to the publishing workflow aren't built yet.

### 1. Install FFmpeg

The tool never installs anything. Check whether FFmpeg is available:

```powershell
.\.venv\Scripts\python.exe reel_creator.py check
```

If it's missing, install it yourself, then close and reopen PowerShell:

```powershell
winget install --id Gyan.FFmpeg --exact
```

You can instead download a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) and add its `bin` folder to your `PATH`.

### 2. Provide your files

| File | Where it goes |
| --- | --- |
| Your video | Anywhere on your computer. You give its path, and the file is only read. It can be `.mp4`, `.mov`, `.m4v`, `.mkv`, `.webm` or `.avi`, and must be 3 seconds to 15 minutes long. |
| Your music | `reels/music/`, which the tool creates the first time it runs. It can be `.mp3`, `.wav`, `.m4a`, `.aac`, `.flac` or `.ogg`. |
| Your licence record | `reels/music/licenses.json`. Copy `music_licenses.example.json` and add one entry per track. |

`reels/` is in `.gitignore`. A track can only be used if its entry has all of these:

| Field | Rule |
| --- | --- |
| `file` | The file name in `reels/music/`. |
| `business_use_verified` | Exactly `true`. Set this only after checking that your licence covers business use on Instagram. |
| `license_type` | `owned`, `purchased_license`, `subscription_license`, `royalty_free_license`, `written_permission`, `cc0` or `cc_by`. Non-commercial licences such as CC BY-NC aren't accepted. |
| `proof` | Where your receipt, licence or written permission is kept. |
| `attribution` | Required for `cc_by`. Keep this text so that you can credit the creator in the caption. |
| `expires` | Optional, as `YYYY-MM-DD`. The track stops being usable after this date. |
| `title`, `artist`, `moods` | Optional. `moods` is a list of your own tags, used for recommendations. |

**The tool can't verify a licence.** It only checks that your record is complete, so its accuracy is your responsibility. The tool never downloads music. Licences vary, and some exclude business accounts or certain platforms, so read yours rather than assuming that "royalty free" is enough. Also, music you add to the file isn't the same as Instagram's in-app music library, so Instagram's licensing doesn't cover it.

### 3. Make a preview

Answer a few questions:

```powershell
.\.venv\Scripts\python.exe reel_creator.py create
```

Or run each step yourself:

```powershell
.\.venv\Scripts\python.exe reel_creator.py music --video "C:\Clips\retreat.mp4" --mood calm,uplifting
.\.venv\Scripts\python.exe reel_creator.py render --video "C:\Clips\retreat.mp4" --track piano.mp3 --music-volume 30 --original-volume 100 --fit pad
```

| Option | Notes |
| --- | --- |
| `--track` | A file name from `reels/music/`. A track without a complete licence record is refused before rendering. |
| `--music-volume` | 1-100, and 30 by default. Music fades out over the last 1.5 seconds. |
| `--original-volume` | 0-100, and 100 by default. 0 mutes the video's own sound. |
| `--fit` | `pad` keeps the whole picture and adds black bars, and is the default. `crop` fills the screen and cuts off the edges. |

Music recommendations use simple rules: your `moods` tags first, then whether the track is long enough. The tool doesn't listen to the audio or use an AI model. A track shorter than the video loops, with a warning.

### 4. Review the result

Each render saves two new files in `reels/previews/`, and never overwrites an existing file:

| File | What it holds |
| --- | --- |
| `<name>-<time>-<id>.mp4` | The Reel: 1080x1920 H.264 and 48 kHz stereo AAC, with GPS and other source metadata removed. This is the full-quality file, so you review exactly what would be used. |
| `<name>-<time>-<id>.json` | The review record: the file's hash, the settings, the licence details, and the results of the checks. |

After rendering, the tool reads the finished file back and checks it against the Reel requirements listed under [Publishing workflow](#1-prepare-a-draft). A status of `awaiting_review` means it passed those checks, and `needs_attention` lists what failed. Neither one means the Reel looks or sounds right, so watch it with sound. Footage from recent iPhones is often HDR, and the tool warns you because its colours may look washed out.

### Local-file drafts

`prepare_instagram_local_reel` binds a draft to one preview in `reels/previews`, so you don't need public hosting. **This upload path has only been tested with mocks and a local test server, never with Meta.** It needs a token from Facebook Login for Business or Unified Login, and whether your token works with it is unconfirmed.

| Argument | Required | Notes |
| --- | --- | --- |
| `file_name` | Yes | A plain file name from `reels/previews`, such as `IMG_6711-20261104-144518-3b06.mp4`, with no folders. |
| `caption` | No | Stored exactly as given, under the same limits as other drafts. |
| `share_to_feed` | No | `true` by default. `false` limits the reel to the Reels tab. |

The tool doesn't read `IG_ACCESS_TOKEN`, so it can't check the caption for it when preparing. That check runs when a publish attempt loads the draft.

| Check | Rule |
| --- | --- |
| File | A plain `.mp4` name directly inside `reels/previews`, not a link, and at most 300 MB. |
| Review record | The matching `.json` must have status `awaiting_review`, name this file, list no problems, and hold the file's SHA-256. |
| Draft hash | The `content_hash` covers the file's name, SHA-256 and size, plus the caption and `share_to_feed`. |
| Every load | The file and its record are checked again. If either changed, the draft is refused, and you prepare a new one. |

`awaiting_review` means the file passed its format checks, not that a person approved it. Your approval is still the phrase you type into the dialog.

Once you confirm in live mode, `publish_instagram_draft` does these in order, and nothing reaches Meta before the phrase:

1. `GET /{account}/content_publishing_limit` checks the quota.
2. `POST /{account}/media` creates a container with `media_type=REELS` and `upload_type=resumable`.
3. The file's record, size and SHA-256 are checked again on the opened file.
4. `POST https://rupload.facebook.com/ig-api-upload/v26.0/{container}` sends the whole file, using `Authorization: OAuth`, `offset: 0` and `file_size`. The token only goes to this fixed address, never to the `uri` in Meta's response.
5. The bytes sent are compared with the approved hash. If they differ, nothing is published.
6. `GET /{container}?fields=status_code` waits for `FINISHED`, then `POST /{account}/media_publish` publishes once.

| If this happens | What the tool does | Calling publish again |
| --- | --- | --- |
| The upload fails, times out, or may not have finished | Abandons that container, and doesn't retry by itself | Asks for the phrase, then starts a new container from byte 0. |
| Meta says `retriable: false` | Marks the draft `failed` | Refuses. Fix the video and prepare a new draft. |
| The file changes before or during the upload | Abandons the container and doesn't publish | Refuses until you prepare a new draft. |
| The upload worked, but processing isn't finished | Keeps the container | Asks for the phrase, then continues without uploading again. |

Interrupted uploads restart from the beginning, because Meta's documentation doesn't explain how to resume one. An abandoned container was never published and expires by itself, so this can't create a duplicate post, but each one counts toward the limit of 400 containers per 24 hours.

After a successful upload, the preview can be deleted without blocking status checks on the post.

### Upload-only test

`test_instagram_local_upload` proves that your token, the upload and Meta's processing work, without a public post. It only runs when `IG_PUBLISHING_MODE` is `upload_test`. In that mode, `publish_instagram_draft` reports `Publishing is off`.

| What it does | What it never does |
| --- | --- |
| Creates an unpublished container, uploads the file once, and reads the processing status. | Sends `media_publish`. The test code has no path to it. |
| Asks you to type `UPLOAD TEST` plus the draft's code. The publish phrase won't work here, and this phrase won't work for publishing. | Retries by itself, or runs without your phrase. |
| Reports the test container's ID in its result. | Changes the draft. A later real publish needs the publish phrase and creates a new container. |

**The file really does leave your computer.** Meta stores it in an unpublished container, which expires after 24 hours. Each run counts toward the limit of 400 containers per 24 hours.

| Result | Meaning |
| --- | --- |
| `upload_test_passed` | Meta's status is `FINISHED`, so the token, upload and processing all worked. |
| `upload_test_inconclusive` | The upload finished, but processing didn't finish within 5 minutes. |
| An error | Nothing was published, and the message says which step failed. |

To run it:

1. Open a new PowerShell window in this folder, and set the mode only there:

    ```powershell
    $env:IG_PUBLISHING_MODE = "upload_test"
    ```

2. In that window, enter the token at the hidden prompt, as described in [Provide the token at startup](#provide-the-token-at-startup).
3. Start Claude Code from that window, and say no if it offers to always allow the tool.
4. Prepare a draft with `prepare_instagram_local_reel`, then ask for an upload test of that exact draft.
5. Check that the dialog starts with `UPLOAD TEST`, and that the file name, size and caption are right. Type the phrase yourself, or decline.
6. Afterward, close that window, or remove both settings:

    ```powershell
    Remove-Item Env:IG_PUBLISHING_MODE
    Remove-Item Env:IG_ACCESS_TOKEN
    ```

### Test it

The mocked tests don't need FFmpeg. Two more tests render synthetic clips with real FFmpeg, and they skip themselves until it's installed, so run this again after installing:

```powershell
.\.venv\Scripts\python.exe -m unittest test_reel_creator -v
```
