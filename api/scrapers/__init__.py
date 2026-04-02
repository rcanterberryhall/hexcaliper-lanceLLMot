# scrapers package — one module per manufacturer
from scrapers.beckhoff        import BeckhoffScraper
from scrapers.allen_bradley   import AllenBradleyScraper
from scrapers.siemens         import SiemensScraper
from scrapers.phoenix_contact import PhoenixContactScraper
from scrapers.danfoss         import DanfossScraper
from scrapers.abb             import ABBScraper
from scrapers.yaskawa         import YaskawaScraper

REGISTRY: dict = {
    "beckhoff":        BeckhoffScraper,
    "allen bradley":   AllenBradleyScraper,
    "rockwell":        AllenBradleyScraper,   # alias
    "siemens":         SiemensScraper,
    "phoenix contact": PhoenixContactScraper,
    "danfoss":         DanfossScraper,
    "abb":             ABBScraper,
    "yaskawa":         YaskawaScraper,
}


def get_scraper(manufacturer: str):
    """Return a scraper instance for the given manufacturer name, or None."""
    cls = REGISTRY.get(manufacturer.lower().strip())
    return cls() if cls else None
