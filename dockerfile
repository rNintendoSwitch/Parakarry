FROM gorialis/discord.py:master

WORKDIR /Parakarry
COPY . /Parakarry

EXPOSE 8880

RUN pip install -r requirements.txt

CMD ["python", "app.py"]