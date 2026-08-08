# Building the SkillRewind proposal

Requirements:

- a recent TeX Live distribution;
- `latexmk`;
- `biber`; and
- the LaTeX packages imported by `main.tex`.

Build from this directory:

```bash
latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build main.tex
```

The generated file is `build/main.pdf`.

The proposal is intentionally labeled as a proposal rather than a results paper. Do not add experimental numbers unless the corresponding benchmark configuration, raw artifacts, and analysis scripts are available.
