import os
import subprocess
import time
import threading
import webbrowser
from win11toast import toast

try:
    # Selenium setup for web apps—allows controlled close
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    _SELENIUM_AVAILABLE = True
except ImportError:
    _SELENIUM_AVAILABLE = False


def open_recommendations(chosen_recommendation: dict) -> str:
    """
    Launches a local app or opens a web app (Selenium-based browser if available).
    Sends a "Time to get back to work" toast and auto-closes after 10 seconds.
    """
    app_name = chosen_recommendation.get("app_name", "Unknown App")
    app_url = chosen_recommendation.get("app_url", "")
    search_query = chosen_recommendation.get("search_query", "")
    is_local = chosen_recommendation.get("is_local", False)

    def notify_and_close_local(proc):
        """
        Sends toast then terminates the process.
        """
        try:
            toast("Reminder", "Time to get back to your work")
        except Exception as ntf_err:
            print(f"[Toast failed] {ntf_err}")
        # give user a moment to see it
        time.sleep(0.5)
        try:
            # terminate(), fallback to kill if needed
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def notify_and_close_driver(driver):
        """
        Sends toast then closes the Selenium driver/browser.
        """
        try:
            toast("Reminder", "Time to get back to your work")
        except Exception as ntf_err:
            print(f"[Toast failed] {ntf_err}")
        time.sleep(0.5)
        try:
            driver.quit()
        except Exception as e:
            print(f"[Driver quit failed] {e}")

    # 1) Local app path
    if is_local:
        if not app_url:
            return f"Error: No path provided for local app '{app_name}'."
        if not os.path.isfile(app_url):
            return f"Error: File not found for local app '{app_name}': {app_url}"

        try:
            print(f"[Launch] {app_name} from {app_url}")
            proc = subprocess.Popen([app_url], shell=False)
        except Exception as ex:
            return f"Error launching local app '{app_name}': {ex}"

        # Schedule close + notification in 10 seconds
        timer = threading.Timer(10.0, notify_and_close_local, args=(proc,))
        timer.daemon = True
        timer.start()

        print(f"Launched local app '{app_name}'. It will close after ~10 seconds.")

        return True

    # 2) Web app
    else:
        if not app_url.startswith(("http://", "https://")):
            return f"Error: Invalid or missing URL for web app '{app_name}': {app_url}"

        # Append search query if present
        if search_query:
            q = search_query.replace(" ", "+")
            delimiter = "&" if "?" in app_url else "?"
            url = f"{app_url}{delimiter}q={q}"
        else:
            url = app_url

        # If Selenium is available, use it to open browser so we can close
        if _SELENIUM_AVAILABLE:
            try:
                options = Options()
                options.add_argument("--start-maximized")
                # launch a separate Chrome window/process
                driver = webdriver.Chrome(options=options)
                driver.get(url)
                print(f"[Selenium Open] {app_name} at {url}")
            except Exception as ex:
                print(f"[Selenium launch failed] {ex}. Falling back to webbrowser.")
                webbrowser.open(url)
                toast("Reminder", f"Opened {app_name}; can't auto-close without Selenium.")
                return f"Opened web app '{app_name}' via default browser (no Selenium)."

            # Schedule close + notification
            timer = threading.Timer(10.0, notify_and_close_driver, args=(driver,))
            timer.daemon = True
            timer.start()

            print(f"Opened web app '{app_name}' with Selenium. It will close after ~10 seconds.")

            return True

        else:
            # No selenium installed; fallback to system default browser
            try:
                webbrowser.open(url)
                print(f"[Webbrowser Open] {app_name} at {url}")
                toast("Reminder",
                      f"Opened {app_name} via default browser; install Selenium to enable auto‑close.")
                return f"Opened '{app_name}' via webbrowser: {url}. No auto‑close due to missing Selenium."
            except Exception as ex:
                print(f"[Webbrowser launch failed] {ex}")
                return False
