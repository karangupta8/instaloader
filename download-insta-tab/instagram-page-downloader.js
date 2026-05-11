/*
  Instagram Page Downloader (console script)
  Usage:
  1) Open an Instagram profile page while logged in.
  2) Open browser DevTools console.
  3) Paste this file and run:
     await runInstagramPageDownloader({ maxPosts: 10, scrollDelayMs: 900 });
*/

async function runInstagramPageDownloader(options = {}) {
  const config = {
    maxPosts: Number.isFinite(options.maxPosts) ? options.maxPosts : 10,
    scrollDelayMs: Number.isFinite(options.scrollDelayMs) ? options.scrollDelayMs : 900,
    requestDelayMs: Number.isFinite(options.requestDelayMs) ? options.requestDelayMs : 350,
    downloadDelayMs: Number.isFinite(options.downloadDelayMs) ? options.downloadDelayMs : 250,
    includeCoverForVideo: Boolean(options.includeCoverForVideo ?? false),
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function postLinksFromDom() {
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    const postHrefRegex = /\/(p|reel)\/[A-Za-z0-9_-]+\/?/;
    const urls = [];
    const seen = new Set();

    for (const anchor of anchors) {
      const href = anchor.getAttribute('href');
      if (!href || !postHrefRegex.test(href)) continue;
      const absolute = new URL(href, location.origin).toString();
      const normalized = absolute.replace(/\?.*$/, '').replace(/\/$/, '');
      if (seen.has(normalized)) continue;
      seen.add(normalized);
      urls.push(normalized);
    }

    return urls;
  }

  async function loadAllVisiblePostLinks() {
    let stableRounds = 0;
    let lastCount = 0;
    let links = postLinksFromDom();

    while (links.length < config.maxPosts && stableRounds < 6) {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });
      await sleep(config.scrollDelayMs);
      links = postLinksFromDom();

      if (links.length === lastCount) {
        stableRounds += 1;
      } else {
        stableRounds = 0;
        lastCount = links.length;
      }
    }

    return links.slice(0, config.maxPosts);
  }

  function extractUsernameFromPathname() {
    const parts = location.pathname.split('/').filter(Boolean);
    return parts.length > 0 ? parts[0] : 'instagram';
  }

  function extensionFromUrl(url, fallback = 'jpg') {
    try {
      const clean = url.split('?')[0];
      const maybeExt = clean.split('.').pop();
      if (!maybeExt || maybeExt.length > 5) return fallback;
      return maybeExt.toLowerCase();
    } catch {
      return fallback;
    }
  }

  async function fetchPostJson(postUrl) {
    const url = `${postUrl}/?__a=1&__d=dis`;
    const response = await fetch(url, {
      credentials: 'include',
      headers: { 'x-requested-with': 'XMLHttpRequest' },
    });

    if (!response.ok) {
      throw new Error(`Failed metadata request (${response.status}) for ${postUrl}`);
    }

    return response.json();
  }

  function extractJsonObjectFromString(str, startIndex) {
    const start = str.indexOf('{', startIndex);
    if (start === -1) return null;
    let depth = 0;
    let inString = false;
    let escaped = false;

    for (let i = start; i < str.length; i += 1) {
      const ch = str[i];

      if (inString) {
        if (escaped) {
          escaped = false;
          continue;
        }
        if (ch === '\\') {
          escaped = true;
          continue;
        }
        if (ch === '"') inString = false;
        continue;
      }

      if (ch === '"') {
        inString = true;
        continue;
      }

      if (ch === '{') depth += 1;
      if (ch === '}') depth -= 1;

      if (depth === 0) {
        return str.slice(start, i + 1);
      }
    }

    return null;
  }

  async function fetchPostHtml(postUrl) {
    const url = `${postUrl}/`;
    const response = await fetch(url, {
      credentials: 'include',
      headers: { 'x-requested-with': 'XMLHttpRequest' },
    });

    if (!response.ok) {
      throw new Error(`Failed post HTML request (${response.status}) for ${postUrl}`);
    }

    return response.text();
  }

  function getMediaNodeFromHtml(html) {
    // Newer IG pages often embed JSON via __additionalDataLoaded(..., { ... })
    const additionalMarker = 'window.__additionalDataLoaded';
    const idx = html.indexOf(additionalMarker);
    if (idx !== -1) {
      const jsonStart = html.indexOf(',', idx);
      const jsonStr = jsonStart !== -1 ? extractJsonObjectFromString(html, jsonStart) : null;
      if (jsonStr) {
        try {
          const parsed = JSON.parse(jsonStr);
          const node = getMediaNode(parsed);
          if (node) return node;
        } catch {
          // ignore and fall back
        }
      }
    }

    // Last-resort: parse OpenGraph tags (works for single-image or video; carousels may be incomplete)
    try {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const ogImage = doc.querySelector('meta[property="og:image"]')?.getAttribute('content') || null;
      const ogVideo = doc.querySelector('meta[property="og:video"]')?.getAttribute('content') || null;
      if (!ogImage && !ogVideo) return null;
      return {
        is_video: Boolean(ogVideo),
        display_url: ogImage,
        video_url: ogVideo,
      };
    } catch {
      return null;
    }
  }

  function getMediaNode(json) {
    return (
      json?.graphql?.shortcode_media ||
      json?.items?.[0] ||
      json?.data?.xdt_shortcode_media ||
      null
    );
  }

  function mediaItemsFromNode(node) {
    if (!node) return [];
    const items = [];

    const pushImage = (url, isCover = false) => {
      if (!url) return;
      items.push({ type: isCover ? 'cover' : 'image', url });
    };

    const pushVideo = (url) => {
      if (!url) return;
      items.push({ type: 'video', url });
    };

    const sidecarEdges =
      node?.edge_sidecar_to_children?.edges || node?.carousel_media || [];

    if (Array.isArray(sidecarEdges) && sidecarEdges.length > 0) {
      for (const entry of sidecarEdges) {
        const child = entry?.node || entry;
        if (!child) continue;
        if (child.is_video) {
          pushVideo(child.video_url || child.video_versions?.[0]?.url);
          if (config.includeCoverForVideo) pushImage(child.display_url, true);
        } else {
          pushImage(child.display_url || child.image_versions2?.candidates?.[0]?.url);
        }
      }
      return items;
    }

    if (node.is_video) {
      pushVideo(node.video_url || node.video_versions?.[0]?.url);
      if (config.includeCoverForVideo) pushImage(node.display_url, true);
    } else {
      pushImage(node.display_url || node.image_versions2?.candidates?.[0]?.url);
    }

    return items;
  }

  async function downloadFile(mediaUrl, fileName) {
    // Media is served from a CDN domain; do not send credentials to avoid CORS issues.
    const response = await fetch(mediaUrl);
    if (!response.ok) {
      throw new Error(`Download failed (${response.status}) for ${mediaUrl}`);
    }

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  }

  const username = extractUsernameFromPathname();
  const links = await loadAllVisiblePostLinks();

  if (!links.length) {
    throw new Error('No post links found. Make sure you are on a profile page with visible posts.');
  }

  console.log(`[IG Downloader] Found ${links.length} post links.`);

  let successCount = 0;
  const failures = [];

  for (let postIndex = 0; postIndex < links.length; postIndex += 1) {
    const postUrl = links[postIndex];
    const shortcode = postUrl.split('/').filter(Boolean).pop() || `post-${postIndex + 1}`;

    try {
      let node = null;
      try {
        const json = await fetchPostJson(postUrl);
        node = getMediaNode(json);
      } catch (error) {
        const message = String(error?.message || error);
        console.warn(`[IG Downloader] JSON metadata failed, trying HTML fallback: ${postUrl}`, message);
        const html = await fetchPostHtml(postUrl);
        node = getMediaNodeFromHtml(html);
      }

      const mediaItems = mediaItemsFromNode(node);

      if (!mediaItems.length) {
        throw new Error('No media found in post metadata.');
      }

      for (let i = 0; i < mediaItems.length; i += 1) {
        const item = mediaItems[i];
        const ext = extensionFromUrl(item.url, item.type === 'video' ? 'mp4' : 'jpg');
        const suffix = mediaItems.length > 1 ? `_${String(i + 1).padStart(2, '0')}` : '';
        const fileName = `${username}_${shortcode}${suffix}.${ext}`;
        await downloadFile(item.url, fileName);
        await sleep(config.downloadDelayMs);
      }

      successCount += 1;
      console.log(`[IG Downloader] Downloaded ${postIndex + 1}/${links.length}: ${shortcode}`);
    } catch (error) {
      failures.push({ postUrl, error: String(error?.message || error) });
      console.warn(`[IG Downloader] Failed ${postUrl}`, error);
    }

    await sleep(config.requestDelayMs);
  }

  const result = {
    totalLinks: links.length,
    downloadedPosts: successCount,
    failedPosts: failures.length,
    failures,
  };

  console.log('[IG Downloader] Finished.', result);
  return result;
}

