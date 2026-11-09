// Adds songs to a Spotify playlist by driving open.spotify.com in a browser.
// Works with a free Spotify account (no Web API / Premium needed).
//
//   node spotify_add.mjs login [apple]       # one-time: log in to Spotify (or Apple Music) in the opened window
//   node spotify_add.mjs apple-token         # used by sync.py; prints the Apple Music login token
//   node spotify_add.mjs add <job.json>      # used by sync.py; prints results as JSON
//
// job.json: {"playlist_name": "...", "dry_run": false, "songs": [{"key", "title", "artist", "duration"}]}

import { chromium } from "playwright";
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROFILE = path.join(HERE, ".spotify-profile");

async function launch(headless) {
  return chromium.launchPersistentContext(PROFILE, {
    channel: "chrome",
    headless,
    ignoreDefaultArgs: ["--use-mock-keychain"], // share cookie encryption with the plain-Chrome login
    locale: "en-US",
    viewport: { width: 1280, height: 900 },
  });
}

const loggedIn = (page) => page.locator('[data-testid="user-widget-link"]').first();

function norm(s) {
  return s
    .normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/\s*[([][^)\]]*(feat|with|remaster|version|edit|mix)[^)\]]*[)\]]/g, "")
    .replace(/\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$/, "")
    .replace(/[^a-z0-9 ]/g, "").replace(/\s+/g, " ").trim();
}

function similarity(a, b) {
  // Dice coefficient on character bigrams.
  if (a === b) return 1;
  const grams = (s) => { const m = new Map(); for (let i = 0; i < s.length - 1; i++) { const g = s.slice(i, i + 2); m.set(g, (m.get(g) || 0) + 1); } return m; };
  const ga = grams(a), gb = grams(b);
  let hit = 0;
  for (const [g, n] of ga) hit += Math.min(n, gb.get(g) || 0);
  const total = Math.max(a.length - 1, 0) + Math.max(b.length - 1, 0);
  return total ? (2 * hit) / total : 0;
}

async function findTrack(page, song) {
  const firstArtist = song.artist.split(/,|&| feat/i)[0].trim();
  await page.goto(`https://open.spotify.com/search/${encodeURIComponent(`${song.title} ${firstArtist}`)}/tracks`);
  const rows = page.locator('[data-testid="tracklist-row"]');
  try {
    await rows.first().waitFor({ timeout: 15000 });
  } catch {
    return null;
  }
  let best = null;
  const count = Math.min(await rows.count(), 6);
  for (let i = 0; i < count; i++) {
    const row = rows.nth(i);
    const link = row.locator('a[href*="/track/"]').first();
    const title = (await link.innerText()).trim();
    const href = await link.getAttribute("href");
    const artists = (await row.locator('a[href*="/artist/"]').allInnerTexts()).join(", ");
    let score = 0.6 * similarity(norm(song.title), norm(title)) + 0.4 * similarity(norm(song.artist), norm(artists));
    if (norm(artists).includes(norm(firstArtist))) score = Math.max(score, 0.6 * similarity(norm(song.title), norm(title)) + 0.4);
    if (!best || score > best.score) best = { row, title, artists, score, id: href.split("/track/")[1].split("?")[0] };
  }
  return best && best.score >= 0.75 ? best : null;
}

async function addToPlaylist(page, row, playlistName, dryRun) {
  await row.click({ button: "right" });
  const menu = page.locator("#context-menu, [data-testid='context-menu']").first();
  await menu.waitFor({ timeout: 5000 });
  await menu.getByText(/^Add to playlist$/i).first().click();
  const search = page.getByPlaceholder(/find a playlist/i).first();
  await search.waitFor({ timeout: 5000 });
  await search.fill(playlistName.replace(/[^\p{L}\p{N}& ]/gu, "").trim());
  const target = page.getByRole("menuitem").filter({ hasText: playlistName.replace(/[^\p{L}\p{N}& ]/gu, "").trim() }).first();
  await target.waitFor({ timeout: 5000 });
  if (dryRun) {
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    return;
  }
  await target.click();
  // Spotify asks for confirmation if the song is already in the playlist; never add duplicates.
  const dup = page.getByRole("button", { name: /don.t add/i });
  if (await dup.waitFor({ timeout: 2000 }).then(() => true, () => false)) await dup.click();
  await page.waitForTimeout(1500);
}

async function login() {
  const site = arg === "apple" ? "Apple Music" : "Spotify";
  const url = arg === "apple" ? "https://music.apple.com/" : "https://open.spotify.com/";
  // Plain Chrome (not automation-controlled), otherwise "Continue with Google" is rejected.
  console.log(`Log in to ${site} in the Chrome window, then quit that window (Cmd+Q).`);
  const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    [`--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check", url],
    { stdio: "ignore" });
  await new Promise((resolve) => chrome.on("exit", resolve));

  const ctx = await launch(true);
  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.goto(url);
  const ok = arg === "apple"
    ? (await ctx.cookies(url)).some((c) => c.name === "media-user-token")
    : await loggedIn(page).waitFor({ timeout: 20000 }).then(() => true, () => false);
  await ctx.close();
  console.log(ok ? `Logged in to ${site}: session saved.` : `Not logged in to ${site} yet; run login again.`);
}

async function remove(playlistId, trackId) {
  // Removes one song from a Spotify playlist (used for testing/cleanup).
  const ctx = await launch(true);
  const page = ctx.pages()[0] || (await ctx.newPage());
  try {
    await page.goto(`https://open.spotify.com/playlist/${playlistId}`);
    const row = page.locator('[data-testid="tracklist-row"]').filter({ has: page.locator(`a[href*="/track/${trackId}"]`) }).first();
    for (let i = 0; i < 40 && !(await row.count()); i++) {
      await page.locator('[data-testid="tracklist-row"]').last().scrollIntoViewIfNeeded().catch(() => {});
      await page.mouse.wheel(0, 1500);
      await page.waitForTimeout(500);
    }
    await row.click({ button: "right" });
    await page.locator("#context-menu, [data-testid='context-menu']").first().getByText(/^Remove from this playlist$/i).first().click();
    await page.waitForTimeout(1500);
    console.log("removed");
  } catch (e) {
    await page.screenshot({ path: path.join(HERE, "spotify_error.png") }).catch(() => {});
    throw e;
  } finally {
    await ctx.close();
  }
}

async function list(playlistId) {
  // Prints every track in the playlist as JSON. The public embed page stops at 100 songs,
  // so this reads the real playlist page and collects the data it loads while scrolling.
  const ctx = await launch(true);
  const items = new Map();
  let total = null;
  try {
    const page = ctx.pages()[0] || (await ctx.newPage());
    page.on("response", async (r) => {
      if (!r.url().includes("pathfinder")) return;
      try {
        const c = (await r.json())?.data?.playlistV2?.content;
        if (!c) return;
        total = c.totalCount ?? total;
        const off = c.pagingInfo?.offset ?? 0;
        c.items.forEach((it, i) => {
          const d = it.itemV2?.data;
          if (d) items.set(off + i, d);
        });
      } catch {}
    });
    await page.goto(`https://open.spotify.com/playlist/${playlistId}`);
    await page.locator('[data-testid="tracklist-row"]').first().waitFor({ timeout: 30000 });
    for (let i = 0; i < 100 && (total === null || items.size < total); i++) {
      await page.locator('[data-testid="tracklist-row"]').last().scrollIntoViewIfNeeded().catch(() => {});
      await page.mouse.wheel(0, 3000);
      await page.waitForTimeout(700);
    }
    await page.waitForTimeout(1000);
  } finally {
    await ctx.close();
  }
  if (total === null || items.size < total) {
    throw new Error(`Only got ${items.size} of ${total ?? "?"} playlist tracks`);
  }
  const tracks = [...items.entries()].sort((a, b) => a[0] - b[0]).map(([, d]) => d)
    .filter((d) => d.uri?.startsWith("spotify:track:"))
    .map((d) => ({
      uri: d.uri,
      title: d.name,
      artist: (d.artists?.items || []).map((a) => a.profile?.name).filter(Boolean).join(", "),
      duration: d.trackDuration?.totalMilliseconds ?? null,
    }));
  console.log(JSON.stringify({ total, tracks }));
}

async function appleToken() {
  // Visiting the web player lets Apple refresh the login cookie before we read it.
  const ctx = await launch(true);
  try {
    const page = ctx.pages()[0] || (await ctx.newPage());
    await page.goto("https://music.apple.com/", { waitUntil: "networkidle" }).catch(() => {});
    const cookie = (await ctx.cookies("https://music.apple.com")).find((c) => c.name === "media-user-token");
    if (!cookie) throw new Error("Not logged in to Apple Music. Run: node spotify_add.mjs login apple");
    process.stdout.write(cookie.value);
  } finally {
    await ctx.close();
  }
}

async function add(jobFile) {
  const job = JSON.parse(readFileSync(jobFile, "utf8"));
  const ctx = await launch(true);
  const page = ctx.pages()[0] || (await ctx.newPage());
  const results = [];
  try {
    await page.goto("https://open.spotify.com/");
    if (!(await loggedIn(page).waitFor({ timeout: 20000 }).then(() => true, () => false))) {
      throw new Error("Not logged in to Spotify. Run: node spotify_add.mjs login");
    }
    for (const song of job.songs) {
      try {
        const hit = await findTrack(page, song);
        if (!hit) { results.push({ key: song.key, status: "not_found" }); continue; }
        await addToPlaylist(page, hit.row, job.playlist_name, job.dry_run);
        results.push({ key: song.key, status: "added", uri: `spotify:track:${hit.id}`, label: `${hit.title} – ${hit.artists}` });
      } catch (e) {
        await page.screenshot({ path: path.join(HERE, "spotify_error.png") }).catch(() => {});
        results.push({ key: song.key, status: "error", error: String(e.message || e).split("\n")[0] });
      }
    }
  } finally {
    await ctx.close();
  }
  console.log(JSON.stringify(results));
}

const [cmd, arg] = process.argv.slice(2);
if (cmd === "login") await login();
else if (cmd === "add") await add(arg);
else if (cmd === "apple-token") await appleToken();
else if (cmd === "list") await list(arg);
else if (cmd === "remove") await remove(arg, process.argv[4]);
else { console.error("usage: node spotify_add.mjs login [apple] | add <job.json> | apple-token"); process.exit(2); }
