# Generated by Django 2.2 on 2019-04-11 12:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('actions', '0006_talkproposal_responder'),
    ]

    operations = [
        migrations.AddField(
            model_name='attendee',
            name='mutual_committment',
            field=models.IntegerField(blank=True, default=0),
        ),
    ]
