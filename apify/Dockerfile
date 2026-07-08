# Apify base image with Python + Playwright + Chromium preinstalled
FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./
RUN echo "Python version:" && python --version \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Installed packages:" && pip freeze

COPY . ./

CMD ["python", "-m", "src"]
