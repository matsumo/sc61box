# title: 4行の表示とカナ・小文字(CURSORの例)
# CURSOR 桁,行で表示の位置(左上が0,0)を決めてからPRINTする。
# WAIT 0にしておくと、PRINTは止まらずに次へ進む。4行目を「>」が右へ進む。
# 小文字は「"」で囲んだ文字列の中に書けば、SHIFT+カナ(SML)に切り替えて打つ。
# BRKで止める。
10 CLS:WAIT 0
20 CURSOR 0,0:PRINT "************************"
30 CURSOR 5,1:PRINT "Hello! PC-1350"
40 CURSOR 2,2:PRINT "ｼｬｰﾌﾟ ﾎﾟｹｯﾄｺﾝﾋﾟｭｰﾀｰ"
50 FOR I=0 TO 22
60 CURSOR I,3:PRINT " >"
70 NEXT I
80 CURSOR 22,3:PRINT "  ":GOTO 50
