.PHONY: test paper clean

test:
	PYTHONPATH=src python -m unittest discover -s tests -v

paper:
	cd paper && latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=build main.tex

clean:
	rm -rf build dist *.egg-info src/*.egg-info paper/build paper/rendered paper/rendered-final
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
