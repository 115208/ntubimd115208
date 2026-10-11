import datetime
from zoneinfo import ZoneInfo
from django.db import models
from .CareStatus import CareStatus
from .PregnancyCase import PregnancyCase

class CareRecord(models.Model):
    carerecord_id = models.AutoField(primary_key=True)
    carestatus = models.ForeignKey(CareStatus, on_delete=models.CASCADE)
    recordtime = models.DateTimeField()
    content = models.CharField(max_length=100, null=True, blank=True)
    state = models.BooleanField(default=False)
    create_time = models.DateTimeField()
    pregnancycase = models.ForeignKey(PregnancyCase, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        db_table = 'carerecord'
        managed = True

    def __str__(self):
        status_label = self.carestatus.carestatus if self.carestatus_id else '-'
        return f"{self.recordtime} {status_label}"