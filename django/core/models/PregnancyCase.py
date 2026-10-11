from django.db import models
from .UserProfile import UserProfile
from django.utils import timezone

#懷孕胎數
class PregnancyCase(models.Model):
    pregnancycase_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE)
    menstruation = models.DateField()
    expecteddate = models.DateField()
    code = models.CharField(max_length=6, unique=True)
    create_time = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'pregnancycase'
        managed = True

    def __str__(self):
        return self.code