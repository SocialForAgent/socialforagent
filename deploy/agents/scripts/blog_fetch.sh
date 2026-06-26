#!/bin/bash
# Wrapper per blog_article_generator.py fetch
# Il cron system non supporta argomenti nello script parameter,
# quindi questo wrapper chiama lo script con l'argomento 'fetch'
cd /opt/data/scripts || exit 1
exec python3 blog_article_generator.py fetch
