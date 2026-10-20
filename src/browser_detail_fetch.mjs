import fs from "node:fs";
import process from "node:process";
import { chromium } from "playwright-core";

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  ].filter(Boolean);
  const executable = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executable) {
    throw new Error("Chrome or Edge was not found. Set CHROME_PATH to the browser executable.");
  }
  return executable;
}

async function main() {
  const [url, selector, minimumLengthText = "500"] = process.argv.slice(2);
  const minimumLength = Number.parseInt(minimumLengthText, 10);
  if (!url || !url.startsWith("https://") || !selector || !Number.isInteger(minimumLength)) {
    throw new Error("Usage: node browser_detail_fetch.mjs <https-url> <selector> [minimum-length]");
  }

  const browser = await chromium.launch({ executablePath: findChrome(), headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector(selector, { state: "attached", timeout: 60000 });
    await page.waitForFunction(
      ({ target, length }) =>
        (document.querySelector(target)?.innerText || "").trim().length >= length,
      { target: selector, length: minimumLength },
      { timeout: 60000 },
    );
    const payload = await page.evaluate((target) => {
      const element = document.querySelector(target);
      return {
        title: document.title,
        url: window.location.href,
        description: (element?.innerText || "").trim(),
      };
    }, selector);
    process.stdout.write(JSON.stringify(payload));
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
