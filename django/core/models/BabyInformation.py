from django.db import models
from .PregnancyCase import PregnancyCase

#小孩資訊
class BabyInformation(models.Model):
    GENDER_CHOICES = (
        ('', '未知'),
        ('1', '男'),
        ('2', '女'),
    )

    baby_id = models.AutoField(primary_key=True)
    pregnancycase = models.ForeignKey(PregnancyCase, on_delete=models.CASCADE)
    name = models.CharField(max_length=20)
    gender = models.CharField(max_length=2, choices=GENDER_CHOICES, blank=True, default='')
    birthdaytime = models.DateTimeField(null=True, blank=True)
    baby_height = models.FloatField(null=True, blank=True)
    baby_weight = models.FloatField(null=True, blank=True)
    babyheadcircumference = models.FloatField(null=True, blank=True)
    chestcircumference = models.FloatField(null=True, blank=True)
    production_method = models.CharField(max_length=10, null=True, blank=True)

    class Meta:
        db_table = 'babyinformation'
        managed = True
    def __str__(self):
        return self.name
