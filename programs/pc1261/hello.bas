# title: 2行の表示とカナ(CURSORの例)
# CURSORで表示の位置(1行目の左端が0、2行目の左端が24)を決めてからPRINTする。
# WAIT 0にしておくと、PRINTは止まらずに次へ進む。2行目を「>」が右へ進む。
# BRKで止める。
10 CLS:WAIT 0
20 CURSOR 1:PRINT "ポケットコンピュータ PC-1261"
30 FOR I=0 TO 22
40 CURSOR 24+I:PRINT " >"
50 NEXT I
60 CURSOR 46:PRINT "  ":GOTO 30
