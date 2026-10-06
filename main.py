from pathlib import Path
import argparse
from urllib.parse import (
    urljoin,
    urlparse,
    parse_qs,
    urlencode,
    urlunparse,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import zipfile

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
MAX_WORKERS = 12

# ============================================================
# CONFIGURAZIONE
# ============================================================

MANGA_URL = "<url della pagina del manga di mangascan>"

# Cartella accanto allo script
OUTPUT_DIR = Path(__file__).resolve().parent / "manga_images"

# CBZ finale
CBZ_PATH = Path(__file__).resolve().parent / "Yofukashi_no_Uta.cbz"

# Domini autorizzati
ALLOWED_HOSTS = {
    "mangaworld.mx",
    "www.mangaworld.mx",
    "cdn.mangaworld.mx",
}


# ============================================================
# CONTROLLO URL
# ============================================================

def is_allowed(url):
    try:
        host = urlparse(url).hostname

        if not host:
            return False

        host = host.lower()

        return (
            host in ALLOWED_HOSTS
            or host.endswith(".mangaworld.mx")
        )

    except ValueError:
        return False


def handle_route(route):
    url = route.request.url

    # Risorse locali
    if url.startswith(("data:", "blob:", "about:")):
        route.continue_()
        return

    # Solo domini autorizzati
    if is_allowed(url):
        route.continue_()
        return

    # Tutto il resto viene bloccato
    route.abort()


# ============================================================
# ?style=list
# ============================================================

def with_style_list(url):
    parsed = urlparse(url)

    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    query["style"] = ["list"]

    return urlunparse(
        parsed._replace(
            query=urlencode(
                query,
                doseq=True,
            )
        )
    )


# ============================================================
# NOME CARTELLA
# ============================================================

def chapter_folder_name(number):

    if isinstance(number, int):
        return f"Capitolo {number:03d}"

    return f"Capitolo {number}"


# ============================================================
# ESTRAZIONE CAPITOLI
# ============================================================

def extract_chapters(page):

    chapters = []

    links = page.locator(
        ".volume-element a.chap"
    ).all()

    for link in links:

        try:
            text = link.locator(
                "span"
            ).inner_text().strip()
        except Exception:
            continue

        # Capitolo 1
        # Capitolo 12
        # Capitolo 182.5

        text = re.sub(
            r"^Capitolo\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        try:
            number = float(text)
        except ValueError:
            continue

        if number.is_integer():
            number = int(number)

        href = link.get_attribute("href")

        if not href:
            continue

        chapter_url = urljoin(
            page.url,
            href,
        )

        # Il capitolo deve essere sul dominio autorizzato
        if not is_allowed(chapter_url):
            continue

        chapters.append({
            "number": number,
            "url": chapter_url,
        })

    # Ordine numerico
    chapters.sort(
        key=lambda chapter: chapter["number"]
    )

    return chapters


# ============================================================
# ESTRAZIONE IMMAGINI
# ============================================================

def extract_images(page):

    images = []
    seen_urls = set()

    for img in page.locator("img").all():

        candidates = (
            img.get_attribute("src"),
            img.get_attribute("data-src"),
            img.get_attribute("data-lazy-src"),
        )

        for src in candidates:

            if not src:
                continue

            src = urljoin(
                page.url,
                src,
            )

            # Ignora immagini inline
            if src.startswith((
                "data:",
                "blob:",
            )):
                continue

            parsed = urlparse(src)

            # Dominio autorizzato
            if not is_allowed(src):
                continue

            # Deve essere un'immagine del capitolo
            if "/chapters/" not in parsed.path:
                continue

            filename = Path(
                parsed.path
            ).name

            # Solo nomi numerici
            match = re.fullmatch(
                r"(\d+)\.(jpg|jpeg|png|webp|gif)",
                filename,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            if src in seen_urls:
                continue

            seen_urls.add(src)

            images.append({
                "number": int(match.group(1)),
                "url": src,
            })

            break

    # Ordine delle pagine
    images.sort(
        key=lambda image: image["number"]
    )

    return images


# ============================================================
# DOWNLOAD IMMAGINE
# ============================================================

def download_image(
    context,
    url,
    output_path,
    referer,
    user_agent,
):

    # Controllo ulteriore
    if not is_allowed(url):

        print(
            f"[BLOCCATA] Dominio non autorizzato: {url}"
        )

        return False

    try:

        response = context.request.get(
            url,
            headers={
                "Referer": referer,
                "User-Agent": user_agent,
                "Accept": (
                    "image/avif,image/webp,"
                    "image/apng,image/svg+xml,"
                    "image/*,*/*;q=0.8"
                ),
            },
            timeout=30000,
        )

        print(
            f"[HTTP] {response.status} {url}"
        )

        if not response.ok:

            print(
                f"[ERRORE HTTP {response.status}]"
            )

            return False

        content_type = response.headers.get(
            "content-type",
            "",
        )

        body = response.body()

        # Evita HTML al posto dell'immagine
        if not content_type.lower().startswith(
            "image/"
        ):

            print(
                "[ERRORE] Content-Type non valido: "
                f"{content_type}"
            )

            return False

        if not body:

            print(
                "[ERRORE] Risposta vuota"
            )

            return False

        output_path.write_bytes(body)

        print(
            f"[SALVATA] {output_path} "
            f"({len(body):,} bytes)"
        )

        return True

    except Exception as exc:

        print(
            f"[ERRORE DOWNLOAD] {url}"
        )

        print(exc)

        return False


# ============================================================
# WORKER DEL CAPITOLO
# ============================================================

def process_chapter(chapter):

    chapter_number = chapter["number"]
    chapter_url = chapter["url"]

    folder_name = chapter_folder_name(
        chapter_number
    )

    chapter_dir = (
        OUTPUT_DIR / folder_name
    )

    chapter_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 70)
    print(
        f"[THREAD] CAPITOLO {chapter_number}"
    )
    print(
        f"[THREAD] {chapter_url}"
    )
    print("=" * 70)

    saved = 0

    # --------------------------------------------------------
    # OGNI THREAD HA IL PROPRIO PLAYWRIGHT
    # --------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
        )

        context = browser.new_context(
            locale="it-IT",
        )

        # Filtro domini
        context.route(
            "**/*",
            handle_route,
        )

        page = context.new_page()

        try:

            # ------------------------------------------------
            # APERTURA READER
            # ------------------------------------------------

            reader_url = with_style_list(
                chapter_url
            )

            print(
                f"[CAP {chapter_number}] "
                f"Apro reader..."
            )

            response = page.goto(
                reader_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            if response is None:

                print(
                    f"[CAP {chapter_number}] "
                    "Nessuna risposta"
                )

                return {
                    "chapter": chapter_number,
                    "saved": 0,
                    "success": False,
                }

            if not response.ok:

                print(
                    f"[CAP {chapter_number}] "
                    f"HTTP {response.status}"
                )

                return {
                    "chapter": chapter_number,
                    "saved": 0,
                    "success": False,
                }

            # ------------------------------------------------
            # ASPETTA LE IMMAGINI
            # ------------------------------------------------

            try:

                page.wait_for_selector(
                    'img[src*="/chapters/"], '
                    'img[data-src*="/chapters/"], '
                    'img[data-lazy-src*="/chapters/"]',
                    timeout=10000,
                )

            except PlaywrightTimeoutError:
                pass

            # Lazy loading
            page.wait_for_timeout(800)

            # ------------------------------------------------
            # ESTRAZIONE
            # ------------------------------------------------

            images = extract_images(page)

            print(
                f"[CAP {chapter_number}] "
                f"Trovate {len(images)} immagini"
            )

            if not images:

                return {
                    "chapter": chapter_number,
                    "saved": 0,
                    "success": False,
                }

            # User-Agent del thread
            user_agent = page.evaluate(
                "navigator.userAgent"
            )

            # ------------------------------------------------
            # DOWNLOAD
            # ------------------------------------------------

            for index, image in enumerate(
                images,
                start=1,
            ):

                image_url = image["url"]

                extension = Path(
                    urlparse(
                        image_url
                    ).path
                ).suffix.lower()

                if extension not in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                    ".gif",
                }:
                    extension = ".jpg"

                filename = (
                    f"{index:05d}"
                    f"{extension}"
                )

                output_path = (
                    chapter_dir / filename
                )

                print(
                    f"[CAP {chapter_number}] "
                    f"[{index:05d}/{len(images):05d}] "
                    f"Pagina {image['number']}"
                )

                success = download_image(
                    context=context,
                    url=image_url,
                    output_path=output_path,
                    referer=page.url,
                    user_agent=user_agent,
                )

                if success:
                    saved += 1

            print(
                f"[CAP {chapter_number}] "
                f"COMPLETATO: {saved}/{len(images)}"
            )

            return {
                "chapter": chapter_number,
                "saved": saved,
                "total": len(images),
                "success": saved == len(images),
            }

        except Exception as exc:

            print(
                f"[CAP {chapter_number}] "
                f"ERRORE: {exc}"
            )

            return {
                "chapter": chapter_number,
                "saved": saved,
                "success": False,
            }

        finally:

            browser.close()


# ============================================================
# CREA CBZ
# ============================================================

def create_cbz():

    print()
    print("=" * 70)
    print("CREAZIONE CBZ")
    print("=" * 70)

    if CBZ_PATH.exists():
        CBZ_PATH.unlink()

    image_files = sorted(
        file
        for file in OUTPUT_DIR.rglob("*")
        if file.is_file()
    )

    if not image_files:

        print(
            "[ERRORE] Nessuna immagine trovata"
        )

        return False

    with zipfile.ZipFile(
        CBZ_PATH,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:

        for file in image_files:

            relative_path = file.relative_to(
                OUTPUT_DIR
            )

            print(
                f"[CBZ] {relative_path}"
            )

            archive.write(
                file,
                arcname=str(relative_path),
            )

    print()
    print(
        f"[CBZ CREATO] {CBZ_PATH}"
    )

    return True


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manga_url", help="URL del manga da scaricare")
    parser.add_argument("--max-workers", type=int, default=5, help="Numero massimo di worker per il download")
    args = parser.parse_args()

    MANGA_URL = args.manga_url
    MAX_WORKERS = args.max_workers
    if not is_allowed(MANGA_URL):
        print(
            f"[ERRORE] URL non valido: {MANGA_URL}"
        )
        return
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ========================================================
    # TROVA I CAPITOLI
    # ========================================================

    print(
        "Apro manga per trovare i capitoli..."
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
        )

        context = browser.new_context(
            locale="it-IT",
        )

        context.route(
            "**/*",
            handle_route,
        )

        page = context.new_page()

        try:

            response = page.goto(
                MANGA_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            if response is None:
                raise RuntimeError(
                    "Nessuna risposta dal server"
                )

            if not response.ok:
                raise RuntimeError(
                    f"HTTP {response.status}"
                )

            page.wait_for_selector(
                ".volume-element",
                timeout=15000,
            )

            chapters = extract_chapters(
                page
            )

        finally:

            browser.close()

    # ========================================================
    # VERIFICA
    # ========================================================

    print()
    print(
        f"Trovati {len(chapters)} capitoli"
    )

    if not chapters:

        print(
            "[ERRORE] Nessun capitolo trovato"
        )

        return

    print(
        "Capitoli:",
        [
            chapter["number"]
            for chapter in chapters
        ],
    )

    print()
    print(
        f"Avvio {len(chapters)} thread..."
    )

    # ========================================================
    # THREAD
    # ========================================================

    results = []

    worker_count = min(
    MAX_WORKERS,
    len(chapters),
    )

    print(
        f"Thread disponibili: {worker_count}" 
    )

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="chapter",
    ) as executor:

        futures = {
            executor.submit(
                process_chapter,
                chapter,
            ): chapter
            for chapter in chapters
        }

        for future in as_completed(futures):

            chapter = futures[future]

            try:

                result = future.result()

                results.append(result)

                print()
                print(
                    f"[THREAD TERMINATO] "
                    f"Capitolo {result['chapter']} "
                    f"-> {result['saved']} immagini"
                )

            except Exception as exc:

                print(
                    f"[THREAD ERRORE] "
                    f"Capitolo {chapter['number']}: "
                    f"{exc}"
                )

    # ========================================================
    # RISULTATI
    # ========================================================

    results.sort(
        key=lambda result: result["chapter"]
    )

    total_saved = sum(
        result.get("saved", 0)
        for result in results
    )

    successful_chapters = sum(
        1
        for result in results
        if result.get("success")
    )

    print()
    print("=" * 70)
    print("DOWNLOAD COMPLETATO")
    print("=" * 70)

    print(
        f"Capitoli completati: "
        f"{successful_chapters}/{len(chapters)}"
    )

    print(
        f"Immagini salvate: {total_saved}"
    )

    print(
        f"Cartella: {OUTPUT_DIR}"
    )

    # ========================================================
    # CBZ
    # ========================================================

    create_cbz()

    print()
    print("=" * 70)
    print("FINE")
    print("=" * 70)


# ============================================================
# AVVIO
# ============================================================

if __name__ == "__main__":
    main()