# Generated by Django 2.2 on 2019-04-14 16:55

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('actions', '0012_action_photos'),
    ]

    operations = [
        migrations.AddField(
            model_name='action',
            name='modified',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name='action',
            name='photos',
            field=models.ManyToManyField(blank=True, to='info.Photo'),
        ),
    ]