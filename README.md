# ZillowAnalyzer

ZillowAnalyzer is a comprehensive toolkit designed for scraping, analyzing, and evaluating real estate data from Zillow. It simplifies the process of gathering property details, estimating market values, and making investment decisions based on a variety of metrics.

## Features

- Scraping property details and Zestimate history from Zillow.
- Calculating real estate investment metrics.
- Leveraging Chrome profiles for more efficient scraping.
- Using SCRAPE_OPS_API_KEY for generating fake profile data.

## Getting Started

### Prerequisites

- Python 3.8+
- pip
- virtualenv (optional but recommended)

### Installation

1. **Clone the repository**

```bash
git clone https://github.com/yourusername/zillowanalyzer.git
cd zillowanalyzer
```

2. **Set up a virtual environment (Optional)**

```bash
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`
```

3. **Install requirements**

```bash
pip install -r requirements.txt
```

4. **Freezing Requirements**

To ensure your project's dependencies are easily replicable, freeze the current state of the environment:

```bash
pip freeze > requirements.txt
```

### Configuration

1. **Chrome Version**

Use `chrome://version/` in your Chrome browser to locate the `user_data_dir` path. Update the path in `zillowanalyzer/scrapers/scraping_utility.py` accordingly.

2. **Environment Variables**

Create a `.env` file in `zillowanalyzer/zillowanalyzer` and retrieve your `SCRAPE_OPS_API_KEY` by following the instructions at [ScrapeOps API Basics](https://scrapeops.io/docs/proxy-aggregator/getting-started/api-basics/). Add the following line to your `.env`:

```env
SCRAPE_OPS_API_KEY=your_api_key_here
```

## Download GoogleChromeForTesting and make it an executable

GoogleChromeForTesting versions are available at [GoogleChromeLabs](https://googlechromelabs.github.io/chrome-for-testing/)
GoogleChromeForTesting 121.0.6167.85 (stable on 02/16/24) is available at:
[121.0.6167.85 ChromeForTesting for Mac Amr64](https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/mac-arm64/chrome-mac-arm64.zip)
[121.0.6167.85 ChromeForTesting for Mac X64](https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/mac-x64/chrome-mac-x64.zip)
[121.0.6167.85 ChromeForTesting for Win64](https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/win64/chrome-win64.zip)

After downloading chrome for testing, rename it so that '.',' ' -> '_', that is periods and spaces are converted to underscores, then make it
executable by running chmod. For example 'GC 121.0.6167.85' can be made an executable by running:

```bash
chmod +x GC_121_0_6167_85.app
```

The driver for this version can be downloaded from GoogleChromeLabs above, additionally a chromedriver for GC 121.0.6167.85 has been included.

## Optionally uncompress existing Data

Optionally uncompress existing data in `zillowanalyzer/Data/HomeData.zip` and `zillowanalyzer/Data/PropertyDetails.zip` to start analyzing right away.

## Usage

To start using ZillowAnalyzer, navigate to the project directory and run:

```bash
python -m zillowanalyzer
```

## Contributing

Contributions are welcome! Please feel free to submit pull requests, report bugs, and suggest features.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

## Acknowledgments

- Thanks to all contributors and users of the project.
- Special thanks to [ScrapeOps](https://scrapeops.io) for providing API keys for fake profile generation.
