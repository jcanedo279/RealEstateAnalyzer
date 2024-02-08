import requests
import socket
import random as rd
from bs4 import BeautifulSoup


PROXY_PROTOCOL = 'https'
PROXY_PORT = 8080
PROXY_PATH = 'proxies.txt'
PROXY_URL = "https://scrapingant.com/proxies"


def load_proxies_from_file():
    with open(PROXY_PATH, 'r') as file:
        return file.readlines()

class ProxyWrapper:
    def __init__(self, ip, port, protocol):
        self.proxy_string = ProxyWrapper.get_proxy_string(ip, port, protocol)

    def get_proxy_string(ip, port, protocol):
        if protocol == "http":
            return f"{ip}:{port}"
        if protocol == "https":
            return f"https://{ip}:{port}"
        elif protocol == "socks4":
            return f"socks4://{ip}:{port}"
        elif protocol == "socks5":
            return f"socks5://{ip}:{port}"

    def format_proxy(ip, port):
        return f"{ip}:{port}"


class ProxyManager:
    def get_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(('10.254.254.254', 1)) # Does not have to be reachable.
            IP = s.getsockname()[0]
        except Exception:
            IP = '127.0.0.1'
        finally:
            s.close()
        return IP

    def __init__(self, include_local_port=False):
        self.fetch_proxy_wrappers()
        self.default_proxy_wrapper = ProxyWrapper(ProxyManager.get_ip(), PROXY_PORT, PROXY_PROTOCOL)
        self.sample_count = 0

        if include_local_port:
            self.proxy_wrappers.add(self.default_proxy_wrapper)
        self.sample_limit = 3 * len(self.proxy_wrappers)
    
    def fetch_proxy_wrappers(self):
        # Make the request to the proxy provider.
        response = requests.get(PROXY_URL)

        # Parse the response using BeautifulSoup.
        soup = BeautifulSoup(response.text, 'html.parser')

        self.proxy_wrappers = []
        # Find all rows in the table (except the header row).
        for row in soup.find_all("tr")[1:]:
            cells = row.find_all("td")
            # Extracting IP, Port, Protocol, and Last Checked time.
            ip = cells[0].text.strip()
            port = cells[1].text.strip()
            protocol = cells[2].text.strip().lower()  # Convert protocol to lowercase.
            last_checked = cells[4].text.strip()

            # Check if the last checked time is within the last minute. If not we still want the five latest proxies.
            if len(self.proxy_wrappers) > 5 and "second" not in last_checked:
                break

            self.proxy_wrappers.append(ProxyWrapper(ip, port, protocol))

    def get_proxy_wrapper(self, use_local_port=False):
        if use_local_port:
            return self.default_proxy
        self.sample_count += 1
        if self.sample_count > self.sample_limit:
            self.fetch_proxy_wrappers()
        return rd.choice(self.proxy_wrappers)
