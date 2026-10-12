"""Reserve a never-reused letter combination before any SMTP attempt."""
import base64
import json
import os
import random
import sys
from scripts.github_api import GitHubAPI, GitHubAPIError
from scripts.dragon_letters import OPENINGS, SCENES, THOUGHTS, EXTRAS, CLOSINGS, TITLES

BRANCH = "mail-bot-state"
PATH = ".mail-bot/used-letters.json"
TOTAL = len(OPENINGS) * len(SCENES) * len(THOUGHTS) * len(CLOSINGS)


def letter(number):
    d = number
    closing = CLOSINGS[d % len(CLOSINGS)]; d //= len(CLOSINGS)
    thought = THOUGHTS[d % len(THOUGHTS)]; d //= len(THOUGHTS)
    scene = SCENES[d % len(SCENES)]; d //= len(SCENES)
    opening = OPENINGS[d]
    rng = random.Random(number ^ 0x5A17)
    extra_count = rng.randint(2, 5)
    extras = rng.sample(EXTRAS, k=min(extra_count, len(EXTRAS)))
    subject = TITLES[number % len(TITLES)]
    return subject, "\n\n".join((opening, scene, *extras, thought, closing))


def mask_for_actions(value):
    if not value:
        return
    safe = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{safe}")


def main():
    api = GitHubAPI(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    endpoint = f"/repos/{api.repo}/contents/{PATH}"
    sha = None
    used = []
    try:
        item = api.request("GET", endpoint + "?ref=" + BRANCH)
        sha = item["sha"]
        data = json.loads(base64.b64decode(item["content"]).decode("utf-8"))
        used = data["used"]
        if not isinstance(used, list) or any(type(n) is not int or not 0 <= n < TOTAL for n in used) or len(set(used)) != len(used):
            raise ValueError("Invalid used-letter ledger")
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise
    available = list(set(range(TOTAL)) - set(used))
    if not available:
        raise RuntimeError("所有信件组合已经用完，停止发送；需要补充新素材。")
    number = random.SystemRandom().choice(available)
    used.append(number)
    payload = {"message": f"Reserve dragon letter {number} before sending", "branch": BRANCH,
               "content": base64.b64encode((json.dumps({"version": 1, "used": used}) + "\n").encode()).decode()}
    if sha:
        payload["sha"] = sha
    api.request("PUT", endpoint, payload)
    subject, body = letter(number)

    # Mask the surprise content before exporting it to later workflow steps.
    mask_for_actions(subject)
    for line in body.splitlines():
        if line.strip():
            mask_for_actions(line)

    env_path = os.environ["GITHUB_ENV"]
    marker = "DRAGON_LETTER_END_" + str(number)
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"MAIL_SUBJECT={subject}\nMAIL_BODY<<{marker}\n{body}\n{marker}\n")
    print(f"已预留信件编号 {number}；发送失败或结果不明时也不会再次使用。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"信件预留失败，已阻止发送：{type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
