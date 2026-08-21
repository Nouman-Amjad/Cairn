#!/usr/bin/env node
import { run } from "../lib/cli.js";

run()
  .then((code) => {
    process.exitCode = code ?? 0;
  })
  .catch((error) => {
    console.error(error?.stack || String(error));
    process.exitCode = 1;
  });
