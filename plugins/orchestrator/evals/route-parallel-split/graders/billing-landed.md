---
# Any reasonable currency parameter passes: the file mentions both `currency` and USD.
type: regex
pattern: '^(?=[\s\S]*currency)(?=[\s\S]*USD)'
target: { source: file, path: billing/src/format.js }
---
