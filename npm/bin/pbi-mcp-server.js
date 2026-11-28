#!/usr/bin/env node
"use strict";

const { runLauncher } = require("../lib/launcher");

runLauncher(process.argv.slice(2));
