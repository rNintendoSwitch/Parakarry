FROM gorialis/discord.py:master

WORKDIR /Parakarry
COPY . /Parakarry

EXPOSE 8880

RUN pip install -U -r requirements.txt

CMD ["python", "app.py"]