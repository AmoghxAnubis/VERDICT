import { createHash } from "node:crypto";
import { readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import solc from "solc";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const normalized = file => readFileSync(file, "utf8").replaceAll("\r\n", "\n");
const settings = {
  optimizer: { enabled: true, runs: 200 },
  viaIR: true,
  evmVersion: "cancun",
  metadata: { bytecodeHash: "none", appendCBOR: false },
  outputSelection: { "*": { "*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object", "evm.deployedBytecode.immutableReferences"] } },
};
const sources = Object.fromEntries(readdirSync(path.join(root, "src"))
  .filter(name => name.endsWith(".sol")).sort()
  .map(name => [`src/${name}`, { content: normalized(path.join(root, "src", name)) }]));
const input = { language: "Solidity", sources, settings };
const output = JSON.parse(solc.compile(JSON.stringify(input), { import: source => {
  if (!source.startsWith("@openzeppelin/contracts/") || source.includes("..")) {
    return { error: `Unapproved import: ${source}` };
  }
  return { contents: normalized(path.join(root, "node_modules", source)) };
} }));
for (const message of output.errors || []) {
  process.stderr.write(message.formattedMessage + "\n");
}
if (output.errors?.some(message => message.severity === "error")) process.exit(1);
const sourceSha256 = Object.fromEntries(Object.entries(sources).map(([name, source]) =>
  [name, createHash("sha256").update(source.content).digest("hex")]));
const packageLockSha256 = createHash("sha256").update(normalized(path.join(root, "package-lock.json"))).digest("hex");
const directory = path.join(root, "artifacts");
if (!process.argv.includes("--check")) mkdirSync(directory, { recursive: true });
for (const name of ["DiagnosticOracle", "FailClosedConsumer"]) {
  const contract = output.contracts[`src/${name}.sol`][name];
  const artifact = JSON.stringify({
    contractName: name, compiler: solc.version(), settings, sourceSha256, packageLockSha256,
    abi: contract.abi, bytecode: `0x${contract.evm.bytecode.object}`,
    deployedBytecode: `0x${contract.evm.deployedBytecode.object}`,
    immutableReferences: contract.evm.deployedBytecode.immutableReferences,
  }, null, 2) + "\n";
  const target = path.join(directory, `${name}.json`);
  if (process.argv.includes("--check")) {
    if (normalized(target) !== artifact) throw new Error(`Stale artifact: ${name}; run npm run compile`);
  } else {
    writeFileSync(target, artifact);
  }
  process.stdout.write(`${name}: ${contract.evm.deployedBytecode.object.length / 2} deployed bytes\n`);
}
