# FB Birthday Bot 🎂

Never miss a friend's birthday on Facebook again. This desktop app checks your
Facebook birthdays page on a schedule and sends each friend a message you write
yourself, either as a private Messenger message or as a timeline post.

If your computer is off or asleep at the scheduled time, the check runs as soon
as the app is running again that day.

> **Unofficial.** This project isn't affiliated with or endorsed by Meta or
> Facebook. It drives a normal browser window on your behalf, and Facebook's terms
> don't allow automated access. Accounts that act like bots can be restricted.
> The app paces itself and keeps volume low, but **use it at your own risk, on
> your own account only.**

## Features

- **Simple GUI:** Dashboard, Messages, Schedule and Settings tabs, no config files needed.
- **Your own messages:** use several templates with `{first_name}`, `{last_name}` and
  `{name}` placeholders (one is picked at random per friend). You can also give
  specific friends a personal message and keep a "never message" list.
- **Preview mode (on by default):** see who would be messaged and with what text,
  without sending anything.
- **Scheduler with catch-up:** set any number of daily check times. Missed times
  run later the same day, and failed runs retry automatically.
- **No double messages:** everyone messaged is logged per day, so retries and
  extra check times skip them.
- **Exact messages:** the bot clears any greeting Facebook pre-fills, and it
  checks the text in the box before pressing Enter.
- **Starts with your computer** (Windows, macOS, Linux) and can run minimized.
- **Safe login:** you log in yourself in a real browser window, 2-factor included.
  Your password is never stored. The app keeps a browser session in its own
  profile folder, the same way your browser stays logged in.

## Requirements

- Python **3.10+**. On Windows, tick **"Add python.exe to PATH"** in the installer.
- Windows, macOS or Linux with a desktop.
- On Linux, tkinter may need installing separately (for example `sudo apt install python3-tk`).

## Install

```bash
git clone https://github.com/<your-username>/fb-birthday-bot.git
cd fb-birthday-bot
pip install -r requirements.txt
python -m playwright install chromium
```

On **Windows** you can double-click `setup_windows.bat` instead of running the
commands above.

> Already have Microsoft Edge or Google Chrome? You can skip the
> `playwright install` step and choose **msedge** or **chrome** under
> *Settings → Browser engine*.

## Run

- **Windows:** double-click `start_windows.bat`
- **macOS / Linux:** `./start_mac_linux.sh`
- **Any platform:** `python fb_birthday_bot.py` (add `--minimized` to start in the background)

## First-time setup

1. **Dashboard → Log in to Facebook…** Log in in the browser window that opens.
   It closes by itself once you're in.
2. **Messages:** write your templates. Double-click one to edit it, or use Add/Edit/Remove.
3. **Schedule:** set your check times (default 09:00) and tick
   **Start automatically when I log in** so it survives reboots.
4. Click **👁 Preview only** and check the *Today* list shows the right friends
   and messages.
5. Happy with it? Untick **Settings → Preview mode**. Runs now send for real.

## How scheduling and catch-up work

- Each check time is a slot for the day. When a slot runs successfully, it's
  recorded on disk.
- Every 15 seconds the app looks for a slot that's past due and hasn't run.
  Several missed slots turn into **one** check.
- Example: your check time is 09:00 and the PC was off until 14:30. The check runs
  at about 14:31. Birthdays only show on the day itself, so a missed day isn't
  caught up after midnight.
- Failed runs (no internet, logged out, etc.) retry every 20 minutes, up to 3
  times. You can change both.
- The app has to be **running**, even if minimized. Closing the window minimizes
  it unless you turn that off. Use **Settings → Quit app** to really exit.

## Troubleshooting

Facebook changes its page layout regularly, and this is the part most likely to
need a tweak.

| Problem | Fix |
|---|---|
| "Couldn't recognise the birthdays page layout" | Your Facebook may not be in English, or the layout changed. Open **Settings → Edit selectors.json** and adjust `today_heading_regex` (for example, the heading text in your language). |
| Messenger asks for a PIN | Click **Log in to Facebook**, open Messenger in that window, enter your chat PIN once, and close it. |
| Logged out / security checks | Turn off **Run hidden** and log in again. |
| Something else | **Settings → Save birthdays-page snapshot** saves a screenshot and the page HTML to the `debug` folder. Failed sends save one automatically too. `bot.log` has the full history. |

`selectors.json` holds every layout-specific setting (heading text, button names,
text-box selectors). It's created in the data folder on first run and read again
on every run, so you don't need to restart after editing it.

**Data folder** (settings, logs, saved session):

- Windows: `%APPDATA%\FBBirthdayBot`
- macOS: `~/Library/Application Support/FBBirthdayBot`
- Linux: `~/.local/share/fb-birthday-bot`

Use **Settings → Log out (forget session)** to delete the saved Facebook session.

## Project layout

```
fb_birthday_bot.py        entry point
birthday_bot/
  app.py                  tkinter GUI
  scheduler.py            daily slots, catch-up, retries
  runner.py               the check job: find birthdays, pick messages, send, log
  fb_client.py            Playwright browser automation (all Facebook-specific code)
  settings.py             JSON settings/state in the per-user data folder
  autostart.py            start-on-login for Windows / macOS / Linux
tests/                    unit tests + an end-to-end test against a fake Facebook
```

## Development

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest
```

The end-to-end test serves fake Facebook pages through Playwright's request
routing, so it never touches the real site. If Facebook's layout changes, start
by saving a snapshot and comparing it with the fake pages in
`tests/test_send_mock.py`.

Pull requests are welcome, especially selector updates when Facebook changes
something, and support for other languages.

## License

[MIT](LICENSE)
