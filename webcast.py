import asyncio
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0"
)

# Use dynamic waits instead of fixed ones. These are the timeouts for those waits.
DYNAMIC_WAIT_TIMEOUT = 15000  # 15 seconds
GAME_TABLE_WAIT_TIMEOUT = 30000 # 30 seconds for the main game list to appear

STREAM_PATTERN = re.compile(r"\.m3u8($|\?)", re.IGNORECASE)
OUTPUT_FILE = "SportsWebcast.m3u8"

NFL_BASE_URL = "https://nflwebcast.com/"
NHL_BASE_URL = "https://slapstreams.com/"
MLB_BASE_URL = "https://mlbwebcast.com/"
MLS_BASE_URL = "https://mlswebcast.com/"
NBA_BASE_URL = "https://nbawebcast.top/"

NFL_CHANNEL_URLS = [
    "http://nflwebcast.com/nflnetwork/",
    "https://nflwebcast.com/nflredzone/",
    "https://nflwebcast.com/espnusa/",
]
MLB_CHANNEL_URLS = []
NHL_CHANNEL_URLS = []
MLS_CHANNEL_URLS = []

CHANNEL_METADATA = {
    "nflnetwork": {"name": "NFL Network", "id": "NFL.Network.HD.us2", "logo": "https://github.com/tv-logo/tv-logos/blob/main/countries/united-states/nfl-network-hz-us.png?raw=true"},
    "nflredzone": {"name": "NFL RedZone", "id": "NFL.RedZone.HD.us2", "logo": "https://github.com/tv-logo/tv-logos/blob/main/countries/united-states/nfl-red-zone-hz-us.png?raw=true"},
    "espnusa": {"name": "ESPN", "id": "ESPN.HD.us2", "logo": "https://github.com/tv-logo/tv-logos/blob/main/countries/united-states/espn-us.png?raw=true"},
}

NBA_STREAM_URL_PATTERN = "https://gg.poocloud.in/{team_name}/tracks-v1a1/mono.ts.m3u8"
NBA_CUSTOM_HEADERS = {
    "origin": "https://ppv.to",
    "referrer": "https://ppv.to/",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
}

def normalize_game_name(original_name: str) -> str:
    """Cleans up game names scraped from the site."""
    cleaned_name = " ".join(original_name.splitlines()).strip()
    if "@" in cleaned_name:
        parts = cleaned_name.split("@")
        if len(parts) == 2:
            team1 = parts[0].strip().title()
            team2 = parts[1].strip().title()
            # FIX: Use a regex to remove any month name and following text.
            team2 = re.split(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\b', team2, 1)[0].strip()
            return f"{team1} @ {team2}"
    return " ".join(cleaned_name.strip().split()).title()

async def verify_stream_url(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None) -> bool:
    """Verifies a stream URL, passing custom headers for origin and referer checks."""
    request_headers = headers if headers else {}
    if "User-Agent" not in request_headers:
        request_headers["User-Agent"] = session.headers.get("User-Agent", USER_AGENT)
        
    try:
        # FIX: Use GET instead of HEAD as some servers block HEAD requests.
        async with session.get(url, timeout=10, allow_redirects=True, headers=request_headers) as response:
            if response.status == 200:
                print(f"    ✔️  URL Verified (200 OK): {url}")
                return True
            else:
                print(f"    ❌ URL Failed ({response.status}) with headers: {url}")
                return False
    except asyncio.TimeoutError:
        print(f"    ❌ URL Timed Out: {url}")
        return False
    except aiohttp.ClientError as e:
        print(f"    ❌ URL Client Error ({type(e).__name__}): {url}")
        return False

async def find_stream_from_servers_on_page(context: BrowserContext, page_url: str, base_url: str, session: aiohttp.ClientSession) -> Optional[str]:
    """
    Navigates to a page, finds the player iframe, clicks through server links 
    *inside* that iframe, and captures the first valid .m3u8 URL.
    """
    # FIX: Define verification headers to bypass server checks.
    verification_headers = {
        "Origin": base_url.rstrip('/'),
        "Referer": base_url
    }
    page = await context.new_page()
    candidate_urls: List[str] = []

    def handle_request(request):
        if STREAM_PATTERN.search(request.url) and request.url not in candidate_urls:
            print(f"    ✅ Captured potential stream: {request.url}")
            candidate_urls.append(request.url)

    page.on("request", handle_request)

    try:
        print(f"    ↳ Navigating to content page: {page_url}")
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        
        # FIX: Use a dynamic wait for network activity to settle.
        print("    Waiting for page network to idle...")
        await page.wait_for_load_state('networkidle', timeout=DYNAMIC_WAIT_TIMEOUT)

        # FIX: Pass verification headers when checking URLs.
        for stream_url in reversed(candidate_urls):
            if await verify_stream_url(session, stream_url, headers=verification_headers):
                print("      ✔️ Found valid stream on initial page load.")
                return stream_url

        # FIX: Locate the iframe where the player and links are loaded.
        iframe_locator = page.locator("div#player iframe, div.vplayer iframe, iframe.responsive-iframe").first
        if not await iframe_locator.count():
            print("    ❌ Could not find the main video iframe on the page.")
            return None

        print("    Found player iframe. Looking for links *inside* it.")
        frame_content = iframe_locator.content_frame

        # Look for links *within the iframe's content*.
        server_links = frame_content.locator("#multistmb a")
        count = await server_links.count()
        if count == 0:
            print("    Fallback: '#multistmb a' not found *inside frame*. Trying generic selector.")
            server_links = frame_content.locator("a:has-text('Server'), a:has-text('HD'), a:has-text('Home'), a:has-text('Away')")
            count = await server_links.count()
        
        print(f"    Found {count} server links to test by clicking.")

        for i in range(count):
            link = server_links.nth(i)
            link_text = (await link.inner_text() or "Unknown Link").strip()
            print(f"    ➡️ Trying Server Link #{i+1}: '{link_text}'")
            
            urls_before_click = set(candidate_urls)
            await link.click()
            
            # FIX: Use dynamic network idle wait after clicking.
            print(f"    Waiting for click network to idle...")
            await page.wait_for_load_state('networkidle', timeout=DYNAMIC_WAIT_TIMEOUT)

            urls_after_click = set(candidate_urls)
            new_urls = list(urls_after_click - urls_before_click)

            # FIX: Pass verification headers when checking newly found URLs.
            for stream_url in reversed(new_urls):
                if await verify_stream_url(session, stream_url, headers=verification_headers):
                    print(f"      ✔️ Found valid stream after clicking '{link_text}'.")
                    return stream_url
            
            print(f"      ❌ No new valid streams found for '{link_text}'.")

    except Exception as e:
        print(f"    ❌ An error occurred while processing {page_url}: {e}")
    finally:
        if not page.is_closed():
            page.remove_listener("request", handle_request)
            await page.close()
            
    print(f"  ❌ All servers tried for {page_url}, but no valid stream was found.")
    return None

async def scrape_league(base_url: str, channel_urls: List[str], group_prefix: str, default_id: str, default_logo: str) -> List[Dict]:
    print(f"\nScraping {group_prefix} streams from {base_url}...")
    found_streams: Dict[str, Tuple[str, str, Optional[str]]] = {}
    results: List[Dict] = []
    
    async with async_playwright() as p, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            page = await context.new_page()
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            
            game_row_selector = "#mtable tr.singele_match_date:not(.mdatetitle)"
            
            # FIX: Use a dynamic wait to ensure the game table is loaded.
            print(f"  Waiting for game table '{game_row_selector}' to load...")
            try:
                await page.wait_for_selector(game_row_selector, timeout=GAME_TABLE_WAIT_TIMEOUT)
                print("  ✅ Game table found.")
            except Exception as e:
                print(f"  ❌ Timed out waiting for game table on {base_url}. No games will be scraped. {e}")
                await page.close()
                await browser.close()
                return []

            game_links_info = []
            event_rows = page.locator(game_row_selector)
            count = await event_rows.count()
            
            for i in range(count):
                row = event_rows.nth(i)
                link_locator = row.locator("td.teamvs a")
                name = await link_locator.inner_text()
                href = await link_locator.get_attribute("href")
                logo_url = None
                logo_locators = row.locator("td.teamlogo img")
                
                if await logo_locators.count() > 1:
                    logo_url = await logo_locators.nth(1).get_attribute("src")
                elif await logo_locators.count() > 0:
                    logo_url = await logo_locators.nth(0).get_attribute("src")
                    
                if name and href:
                    unique_name_key = " ".join(name.splitlines()).strip()
                    full_url = urljoin(base_url, href)
                    final_logo_url = logo_url if logo_url else default_logo
                    game_links_info.append({"name": unique_name_key, "url": full_url, "logo": final_logo_url})
                    
            await page.close()
            
            print(f"  Found {len(game_links_info)} potential live game links.")
            
            for game in game_links_info:
                print(f"  ➡️ Processing Game: {normalize_game_name(game['name'])}")
                stream_url = await find_stream_from_servers_on_page(context, game["url"], base_url, session)
                if stream_url:
                    found_streams[game["name"]] = (stream_url, "Live Games", game["logo"])
            
            for url in channel_urls:
                slug = url.strip("/").split("/")[-1]
                print(f"  ➡️ Processing Channel: {slug}")
                stream_url = await find_stream_from_servers_on_page(context, url, base_url, session)
                if stream_url:
                    found_streams[slug] = (stream_url, "24/7 Channels", None)
                    
        except Exception as e:
            print(f"  ❌ A critical error occurred while scraping {group_prefix}: {e}")
        finally:
            await browser.close()

    for slug, data_tuple in sorted(found_streams.items()):
        stream_url, category, scraped_logo = data_tuple
        
        info = CHANNEL_METADATA.get(slug, {})
        pretty_name = info.get("name", normalize_game_name(slug))
        tvg_id = info.get("id", default_id)
        tvg_logo = info.get("logo") or scraped_logo or default_logo
        
        results.append({"name": pretty_name, "url": stream_url, "tvg_id": tvg_id, "tvg_logo": tvg_logo, "group": f"{group_prefix} - {category}", "ref": base_url})
        
    return results

async def scrape_nba_league(default_logo: str) -> List[Dict]:
    print(f"\nScraping NBAWebcast streams from {NBA_BASE_URL}...")
    results: List[Dict] = []
    
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            async with session.get(NBA_BASE_URL, timeout=20) as response:
                response.raise_for_status()
                html_content = await response.text()
        except aiohttp.ClientError as e:
            print(f"  ❌ Error fetching NBA page: {e}")
            return []
            
        soup = BeautifulSoup(html_content, 'lxml')
        schedule_table = soup.find('table', class_='NBA_schedule_container')
        
        if not schedule_table:
            print("  ❌ Could not find NBA schedule table (it may be loaded by JavaScript).")
            return []
            
        game_rows = schedule_table.find('tbody').find_all('tr')
        print(f"  Found {len(game_rows)} potential NBA games.")
        
        for row in game_rows:
            try:
                teams = [span.text for span in row.find_all('td', class_='teamvs')]
                away_team, home_team = teams[0].strip(), teams[1].strip()
                
                logos = row.find_all('td', class_='teamlogo')
                logo_url = logos[1].find('img')['src'] if len(logos) > 1 and logos[1].find('img') else default_logo
                
                watch_button = row.find('button', class_='watch_btn')
                if watch_button and watch_button.has_attr('data-team'):
                    team_key = watch_button['data-team']
                    stream_url = NBA_STREAM_URL_PATTERN.format(team_name=team_key)
                    match_title = f"{away_team} vs {home_team}"
                    
                    # Pass the correct headers for NBA verification
                    if await verify_stream_url(session, stream_url, headers=NBA_CUSTOM_HEADERS):
                        results.append({
                            "name": match_title,
                            "url": stream_url,
                            "tvg_id": "NBA.Basketball.Dummy.us",
                            "tvg_logo": logo_url,
                            "group": "NBAWebcast - Live Games",
                            "ref": NBA_BASE_URL,
                            "custom_headers": NBA_CUSTOM_HEADERS,
                        })
            except (AttributeError, IndexError) as e:
                print(f"  ⚠️ Could not parse an NBA game row, skipping. Error: {e}")
            
    return results

def write_playlist(streams: List[Dict], filename: str):
    if not streams:
        print("⏹️ No streams found to write to the playlist.")
        return
        
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for entry in streams:
            extinf_line = (
                f'#EXTINF:-1 tvg-id="{entry["tvg_id"]}" '
                f'tvg-name="{entry["name"]}" '
                f'tvg-logo="{entry["tvg_logo"]}" '
                f'group-title="{entry["group"]}",{entry["name"]}\n'
            )
            f.write(extinf_line)
            
            if "custom_headers" in entry:
                headers = entry["custom_headers"]
                f.write(f'#EXTVLCOPT:http-origin={headers["origin"]}\n')
                f.write(f'#EXTVLCOPT:http-referrer={headers["referrer"]}\n')
                f.write(f'#EXTVLCOPT:http-user-agent={headers["user_agent"]}\n')
            else:
                f.write(f'#EXTVLCOPT:http-origin={entry["ref"]}\n')
                f.write(f'#EXTVLCOPT:http-referrer={entry["ref"]}\n')
                f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
                
            f.write(entry["url"] + "\n")
            
    print(f"✅ Playlist with {len(streams)} streams saved successfully to {filename}!")

async def main():
    print("🚀 Starting Sports Webcast Scraper...")
    NBA_DEFAULT_LOGO = "http://drewlive24.duckdns.org:9000/Logos/Basketball.png"
    
    tasks = [
        scrape_league(
            base_url=NFL_BASE_URL, channel_urls=NFL_CHANNEL_URLS, group_prefix="NFLWebcast",
            default_id="NFL.Dummy.us", default_logo="http://drewlive24.duckdns.org:9000/Logos/Maxx.png"
        ),
        scrape_league(
            base_url=NHL_BASE_URL, channel_urls=NHL_CHANNEL_URLS, group_prefix="NHLWebcast",
            default_id="NHL.Hockey.Dummy.us", default_logo="http://drewlive24.duckdns.org:9000/Logos/Hockey.png"
        ),
        scrape_league(
            base_url=MLB_BASE_URL, channel_urls=MLB_CHANNEL_URLS, group_prefix="MLBWebcast",
            default_id="MLB.Baseball.Dummy.us", default_logo="http://drewlive24.duckdns.org:9000/Logos/MLB.png"
        ),
        scrape_league(
            base_url=MLS_BASE_URL, channel_urls=MLS_CHANNEL_URLS, group_prefix="MLSWebcast",
            default_id="MLS.Soccer.Dummy.us", default_logo="http://drewlive24.duckdns.org:9000/Logos/Football2.png"
        ),
        scrape_nba_league(default_logo=NBA_DEFAULT_LOGO),
    ]
    
    results = await asyncio.gather(*tasks)
    
    all_streams = [stream for league_streams in results for stream in league_streams]
    
    write_playlist(all_streams, OUTPUT_FILE)

if __name__ == "__main__":
    asyncio.run(main())
