# Generated by Django 2.2 on 2019-05-16 20:57

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('circles', '0010_auto_20190516_1656'),
    ]

    operations = [
        migrations.AddField(
            model_name='circle',
            name='members',
            field=models.ManyToManyField(blank=True, through='circles.CircleMember', to='contacts.Contact'),
        ),
    ]
