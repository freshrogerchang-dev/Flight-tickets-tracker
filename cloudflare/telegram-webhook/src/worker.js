/**
 * Telegram webhook -> instant GitHub Actions trigger.
 *
 * Telegram's Bot API is push-based when a webhook is registered: it POSTs
 * every update here the moment it happens, instead of this project having to
 * poll `getUpdates` on a schedule (which is what `.github/workflows/commands.yml`
 * does today, capped at every ~30 minutes by the cron interval).
 *
 * This Worker deliberately does NOT reimplement any of routes.yaml's parsing,
 * validation or git-committing -- that logic lives in `tracker/commands.py`
 * and stays there. All this does is recognise "this looks like a real
 * command" and ask GitHub to run the `commands` workflow right now, passing
 * the message straight through as a workflow_dispatch input. The workflow
 * applies it exactly the way a polled message would be applied.
 *
 * Important Telegram constraint this design works around: once a webhook is
 * registered for a bot, Telegram refuses every `getUpdates` call (HTTP 409)
 * -- polling and webhook delivery are mutually exclusive for one bot. So the
 * workflow run this triggers must NOT poll; it applies the one message it
 * was handed via `tracker.cli commands --message`. See the README section
 * this directory is documented under for the full picture, including why
 * the old cron poll needs to be retired once this is in place.
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    // Telegram sends this header on every webhook delivery, IF the webhook
    // was registered with a secret_token (see the README's setWebhook step).
    // It is the only proof a request actually came from Telegram rather than
    // anyone who found this Worker's URL.
    const suppliedSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TELEGRAM_WEBHOOK_SECRET || suppliedSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("ok", { status: 200 }); // malformed body, nothing to do
    }

    const text = update?.message?.text ?? "";
    const updateId = update?.update_id;

    // Pre-filter with the same shared secret tracker.commands.parse() itself
    // requires: idle chatter (a "hi", a sticker, someone else in the chat)
    // never spends a GitHub Actions run. This is only an optimisation --
    // the real authorisation check still happens in Python on the other end,
    // exactly as it does for the polled path, so this being bypassed some
    // other way is not a security hole, just a wasted Actions run.
    if (!env.COMMAND_SECRET || !text.startsWith(env.COMMAND_SECRET)) {
      // Visible only via `wrangler tail` -- never the message text itself,
      // just enough to tell "ignored" apart from "dispatched" while debugging.
      console.log(
        env.COMMAND_SECRET
          ? `ignored: message does not start with COMMAND_SECRET (length ${text.length})`
          : "ignored: COMMAND_SECRET is not set on this Worker"
      );
      return new Response("ok", { status: 200 });
    }
    console.log(`matched COMMAND_SECRET, dispatching update_id=${updateId}`);

    const dispatchUrl =
      `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
      `/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;

    const response = await fetch(dispatchUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "flight-tickets-tracker-telegram-webhook",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: env.GITHUB_REF,
        inputs: {
          message: text,
          message_id: updateId === undefined ? "" : String(updateId),
        },
      }),
    });

    if (!response.ok) {
      // Telegram retries a webhook delivery on a non-2xx response, which
      // would just re-fire this same dispatch attempt repeatedly for a
      // transient GitHub-side failure. Log it and still ack -- there is no
      // polling fallback once a webhook is active (see the module docstring),
      // so a dispatch failure here means this one message is lost; that
      // trade-off is the whole point of switching away from polling.
      console.error(`workflow_dispatch failed: ${response.status} ${await response.text()}`);
    }

    return new Response("ok", { status: 200 });
  },
};
