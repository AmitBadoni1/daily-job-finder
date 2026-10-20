import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const CAREERS_URL = "https://www.tesla.com/careers/search/";
const BOARD_PATH = "/cua-api/apps/careers/state";
const DETAIL_PATH = "/cua-api/careers/job/";
const PROFILE_DIRECTORY = fileURLToPath(
  new URL("../data/tesla_browser_profile/", import.meta.url),
);

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  ].filter(Boolean);
  const executable = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executable) {
    throw new Error("Chrome or Edge was not found. Set CHROME_PATH to the browser executable.");
  }
  return executable;
}

function parseArguments(argv) {
  const args = argv.slice(2);
  const summary = args.includes("--summary");
  const positional = args.filter((arg) => !arg.startsWith("--"));
  const operation = positional[0] || "board";
  const jobId = positional[1] || "";
  const unknownFlags = args.filter(
    (arg) => arg.startsWith("--") && arg !== "--summary",
  );

  if (unknownFlags.length || !new Set(["board", "detail"]).has(operation)) {
    throw new Error(
      "Usage: node tesla_browser_fetch.mjs board|detail [job-id] [--summary]",
    );
  }
  if (operation === "detail" && !/^\d+$/.test(jobId)) {
    throw new Error("The detail operation requires a numeric Tesla job ID.");
  }
  return { operation, jobId, summary };
}

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve) => server.close(resolve));
  if (!port) throw new Error("Could not reserve a Chrome debugging port.");
  return port;
}

async function waitForChrome(port) {
  const endpoint = `http://127.0.0.1:${port}/json/version`;
  for (let attempt = 1; attempt <= 30; attempt += 1) {
    try {
      const response = await fetch(endpoint);
      if (response.ok) return;
    } catch {
      // Chrome is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("Chrome did not open its local debugging port.");
}

async function fetchJsonInPage(page, path) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const result = await page.evaluate(async (requestPath) => {
      const response = await fetch(requestPath, {
        headers: { Accept: "application/json" },
        credentials: "include",
      });
      return {
        status: response.status,
        contentType: response.headers.get("content-type") || "",
        body: await response.text(),
      };
    }, path);

    if (result.status === 200 && result.contentType.includes("application/json")) {
      return JSON.parse(result.body);
    }
    if (attempt < 3) await page.waitForTimeout(5000);
  }
  throw new Error("Tesla blocked the careers request after three attempts.");
}

async function main() {
  const options = parseArguments(process.argv);
  fs.mkdirSync(PROFILE_DIRECTORY, { recursive: true });
  const port = await reservePort();
  const chromeProcess = spawn(
    findChrome(),
    [
      `--remote-debugging-port=${port}`,
      "--remote-debugging-address=127.0.0.1",
      `--user-data-dir=${PROFILE_DIRECTORY}`,
      "--no-first-run",
      "--no-default-browser-check",
      CAREERS_URL,
    ],
    { stdio: "ignore" },
  );

  let browser;

  try {
    await waitForChrome(port);
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    const context = browser.contexts()[0];
    const page = context.pages()[0] || (await context.newPage());
    await page.waitForTimeout(8000);

    const path =
      options.operation === "board" ? BOARD_PATH : `${DETAIL_PATH}${options.jobId}`;
    const payload = await fetchJsonInPage(page, path);

    if (options.summary && options.operation === "board") {
      const listings = Array.isArray(payload.listings) ? payload.listings : [];
      process.stdout.write(
        JSON.stringify({ count: listings.length, sample: listings[0] || null }, null, 2),
      );
    } else if (options.summary) {
      process.stdout.write(
        JSON.stringify(
          {
            id: payload.id || null,
            title: payload.title || null,
            location: payload.location || null,
            timeType: payload.timeType || null,
          },
          null,
          2,
        ),
      );
    } else {
      process.stdout.write(JSON.stringify(payload));
    }
  } finally {
    if (browser) await browser.close();
    if (!chromeProcess.killed) chromeProcess.kill();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
