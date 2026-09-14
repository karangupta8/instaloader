#!/usr/bin/env python3
"""
scrape_comments.py — dump every comment (and reply) on a single Instagram post
to a plain text file, using the parent Instaloader package for the network layer.

Usage:
    python download-insta-tab/scrape_comments.py <shortcode-or-url> \\
        --sessionid <SESSIONID> --csrftoken <CSRFTOKEN> \\
        [--output comments.txt]

Comments require an authenticated session (Instagram does not expose them
anonymously), so pass the same --sessionid/--csrftoken cookies used by
server.py (DevTools → Application → Cookies → instagram.com).
"""

import argparse
import itertools
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

# Import from parent instaloader package without modifying it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instaloader

SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel)/([^/?#]+)")


def _extract_shortcode(value: str) -> str:
    match = SHORTCODE_RE.search(value)
    return match.group(1) if match else value


def _user_id_from_sessionid(sessionid: str) -> Optional[int]:
    """Instagram's sessionid cookie is "<user_id>:<token>:<version>:<hash>" (colons may be
    percent-encoded), so the numeric user id can be read straight out of it with no extra request."""
    match = re.match(r"^(\d+)[:%]", urllib.parse.unquote(sessionid))
    return int(match.group(1)) if match else None


def _authenticate(loader: instaloader.Instaloader, sessionid: str, csrftoken: str) -> str:
    loader.context.load_session("unknown", {"sessionid": sessionid, "csrftoken": csrftoken})
    username = loader.test_login()
    if not username:
        raise SystemExit("Session cookies are invalid or expired. Re-copy them from DevTools and try again.")
    loader.context.username = username
    # login() normally sets this from Instagram's response; load_session() (raw cookies) doesn't,
    # so the iPhone comments endpoint (used for posts with many comments) sends "ig-intended-user-id: None"
    # and Instagram rejects it with a generic "fail" response. Parse it from the sessionid cookie instead
    # of querying the profile endpoint, which is a separate call that gets rate-limited (429) quickly.
    user_id = _user_id_from_sessionid(sessionid)
    if user_id is None:
        raise SystemExit("Could not parse a user id out of --sessionid; make sure you copied the full cookie value.")
    loader.context.user_id = user_id
    print(f"[scrape-comments] Authenticated as @{username}")
    return username


def scrape_comments(post: "instaloader.Post", output_path: Path, max_comments: Optional[int] = None) -> int:
    """Write up to `max_comments` top-level comments (and their replies) on `post` to `output_path`.

    `max_comments=None` writes all of them. Returns the total number of lines written
    (top-level comments plus replies).
    """
    written = 0
    comments = post.get_comments()
    if max_comments is not None:
        comments = itertools.islice(comments, max_comments)
    with output_path.open("w", encoding="utf-8") as fh:
        for comment in comments:
            fh.write(
                f"@{comment.owner.username} ({comment.likes_count} likes, "
                f"{comment.created_at_utc.isoformat()}):\n{comment.text}\n\n"
            )
            written += 1
            for answer in comment.answers:
                fh.write(
                    f"    ↳ @{answer.owner.username} ({answer.likes_count} likes, "
                    f"{answer.created_at_utc.isoformat()}):\n    {answer.text}\n\n"
                )
                written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("post", help="Post shortcode or full instagram.com/p/<shortcode>/ URL")
    parser.add_argument("--sessionid", required=True, help="Instagram sessionid cookie value")
    parser.add_argument("--csrftoken", required=True, help="Instagram csrftoken cookie value")
    parser.add_argument("--output", "-o", help="Output .txt path (default: <shortcode>_comments.txt)")
    parser.add_argument("--max-comments", "-n", type=int, default=None,
                        help="Only fetch the top N top-level comments (default: all of them)")
    args = parser.parse_args()

    if args.max_comments is not None and args.max_comments <= 0:
        raise SystemExit("--max-comments must be a positive integer")

    shortcode = _extract_shortcode(args.post)
    output_path = Path(args.output) if args.output else Path(f"{shortcode}_comments.txt")

    loader = instaloader.Instaloader()
    _authenticate(loader, args.sessionid, args.csrftoken)

    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    limit_desc = f"top {args.max_comments}" if args.max_comments else "all"
    print(f"[scrape-comments] Fetching {limit_desc} comments for @{post.owner_username}/{shortcode} "
          f"(~{post.comments} top-level comments reported by Instagram)...")

    count = scrape_comments(post, output_path, max_comments=args.max_comments)
    print(f"[scrape-comments] Wrote {count} comments/replies to {output_path}")


if __name__ == "__main__":
    main()
