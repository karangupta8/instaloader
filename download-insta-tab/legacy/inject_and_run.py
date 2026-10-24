#!/usr/bin/env python3
"""
Inject console.js into a running browser tab and run the igdl command.
Requires Chrome to be running with remote debugging enabled:
    chrome.exe --remote-debugging-port=9222
"""

import argparse
import os
import sys
from playwright.sync_api import sync_playwright

def main():
    parser = argparse.ArgumentParser(description="Inject console.js and run igdl in a browser tab.")
    parser.add_argument("--count", type=int, default=10, help="Number of posts to download")
    parser.add_argument("--skip", type=int, default=0, help="Number of posts to skip")
    parser.add_argument("--port", type=int, default=7432, help="Server port")
    parser.add_argument("--tab", type=int, help="Index of the tab to attach to (0-based)")
    parser.add_argument("--url", default="http://localhost:9222", help="CDP URL")
    args = parser.parse_args()

    # Read console.js
    script_path = os.path.join(os.path.dirname(__file__), "console.js")
    if not os.path.exists(script_path):
        print(f"ERROR: console.js not found at {script_path}")
        sys.exit(1)
        
    with open(script_path, "r", encoding="utf-8") as f:
        script_content = f.read()

    try:
        with sync_playwright() as p:
            print(f"Connecting to browser at {args.url}...")
            browser = p.chromium.connect_over_cdp(args.url)
            
            # Find pages
            pages = []
            for context in browser.contexts:
                pages.extend(context.pages)
                
            if not pages:
                print("No open tabs found!")
                return
                
            target_idx = args.tab
            if target_idx is None or target_idx < 0 or target_idx >= len(pages):
                print("\nAvailable tabs:")
                for i, page in enumerate(pages):
                    print(f"[{i}] {page.title()} ({page.url})")
                
                try:
                    target_idx = int(input("\nEnter tab index to attach to: "))
                except ValueError:
                    print("Invalid input.")
                    return
                    
                if target_idx < 0 or target_idx >= len(pages):
                    print("Invalid index.")
                    return
                    
            target_page = pages[target_idx]
            print(f"\nAttaching to: {target_page.title()}")
            
            # Listen to console logs
            target_page.on("console", lambda msg: print(f"[Browser] {msg.text}"))
            
            # Inject script
            print("Injecting console.js...")
            target_page.evaluate(script_content)
            
            # Run igdl
            print(f"Running igdl(count={args.count}, skip={args.skip}, port={args.port})...")
            
            js_call = f"igdl({{ count: {args.count}, skip: {args.skip}, port: {args.port} }})"
            
            try:
                # evaluate awaits the promise returned by the async function
                target_page.evaluate(f"async () => await {js_call}")
                print("\nigdl finished successfully!")
            except Exception as e:
                print(f"\nError during igdl execution: {e}")
                
    except Exception as e:
        print(f"Failed to connect or execute: {e}")
        print("\nTip: Make sure Chrome is running with remote debugging enabled:")
        print('chrome.exe --remote-debugging-port=9222')

if __name__ == "__main__":
    main()
