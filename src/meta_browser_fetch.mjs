import fs from "node:fs";
import process from "node:process";
import { chromium } from "playwright-core";

const CAREERS_URL = "https://www.metacareers.com/jobsearch/";

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
  const browser = await chromium.launch({ executablePath: findChrome(), headless: true });
  try {
    const page = await browser.newPage();
    let resolveResults;
    let rejectResults;
    const resultsPromise = new Promise((resolve, reject) => {
      resolveResults = resolve;
      rejectResults = reject;
    });

    page.on("response", async (response) => {
      try {
        const request = response.request();
        const body = request.postData() || "";
        if (!body.includes("CareersJobSearchResultsV2DataQuery")) return;
        const payload = await response.json();
        const search = payload?.data?.job_search_with_featured_jobs_v2;
        if (!search || !Array.isArray(search.all_jobs)) {
          throw new Error("Meta search response is missing all_jobs");
        }
        resolveResults({
          listings: search.all_jobs,
          jobCount: Number.isInteger(search.job_count)
            ? search.job_count
            : search.all_jobs.length,
        });
      } catch (error) {
        rejectResults(error);
      }
    });

    await page.goto(CAREERS_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
    const result = await Promise.race([
      resultsPromise,
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("Meta job search timed out")), 60000),
      ),
    ]);
    process.stdout.write(
      JSON.stringify({ job_count: result.jobCount, listings: result.listings }),
    );
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
