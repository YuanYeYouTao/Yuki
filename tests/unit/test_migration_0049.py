"""Canonical 3.8 baseline and fail-closed 0049 bridge coverage."""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

_BRIDGE_PATH = Path("migrations/versions/0049_canonical_only_bridge.py")
_RETIRED_TABLES = {
    "conversation_rollup_emergency_overlays",
    "conversation_rollup_jobs",
    "conversation_rollups",
    "conversation_scopes",
    "groups",
    "identity_backfill_runs",
    "identity_conflicts",
    "identity_cutover_manifests",
    "identity_cutover_runs",
    "identity_runtime_state",
    "people",
}
_FTS_TRIGGERS = {
    "chat_events_fts_ad",
    "chat_events_fts_ai",
    "chat_events_fts_au",
    "memory_facts_fts_ad",
    "memory_facts_fts_ai",
    "memory_facts_fts_au",
}

# A compressed SQL fixture made from the frozen production-shaped 0048 schema.
# It contains representative canonical rows plus the retired identity carriers,
# but no secrets, tokens, paths, or real external account identifiers.
_HISTORICAL_0048_B85 = """
ABzY8!xWBc0{`v3X_FgAk|6qd^(!#^0+zihE$&0pY%@wKX}GH<dnsxT8ykb=5va<p1QN;wNL9D>e}8e`R{|)oC?%&yBr+r1BO^TA
Jv{vQ>-*O~{^fOVUDcqt+pZqKWAp0W4?q0tSKr?L_~z}ue)Zk^+t(j%dmmnZ`_pYN$v4GzRqa5X?2Br<0w2M4-}GL7^}mX|_vY<~
+aGV=_ujty(0lvKPd{DvzWQHDwy)~dp#k*@w)pqg@4x%E*Y97>CRa+UeO=t$!6qBfG|3&1n+`{dtKRpwKfM0srw_e;U+I=r+mE1b
$WY+Gd8g5I>h6^6lC&s`{iAa@{QJ~VC?ihlJFs8vA9uk0RisVi$D{x)!Rl`fMEl|PKR<|e+e2BZZIksL&Q?^@=8$D@R(s#Rd-v1r
>$l1~P}gvAvSbhLs#=*2^fo`xMQ#9`=)CFu@YB23AJn<50qmTwl6~)cNF*QL{N+}8|JV0#{_^_$fA#+F?SJ)N7C8p{-MhCxe|Y~I
5=$?8SlJ0`#c=n&`?uTg{%`N4F_u5S>J6^Gy3)z>4)$I_nuAS`qDuPGyY#*pjhHk4q_>?>a|%kZNsDZSHU|0(I0^KhtL<UK(Tzvy
M@g%qL0Ih`vYvugf@i^h*jF1e2lc_f2%@E<MU&lw{7}N_fLp^pA8@e_Nx5p^Uh_@cy4V)Yz1jd{*eCl#Bi4GR&PtJk&92&mZT6_}
7n3ITVArg&YO^Z=90&gzzA*>tt}M$9B!RMQf-Uz|RYo?Y+_Qnix7`O@`{}R-&7s_{n!{$3)Q>h+^N)Y_&&>-|(pk~Pp)7#@dh@pT
vJYQxi|t+iy4T0M5#%68Kh{Z6l7BT1#coIbxha~4{OhpIK)p|jE&7p`RrUa%U*VDb@b3NXn;+kz(Y;g%^Q!m$_J`Z|w{O3@{aGUJ
27g2E-CH;zKixv=|L*nA-@X3+R{N^Muf|vDH1*5dH~;UKTLeydeieqKZVv`W8U1xnY2@9mh=xOZ7QS|FR@7bMraFhQ$X&}e@^8oO
?$C-RtHZu!k7F5$e>d4^U#2)RtqxM7Yhoog-+EFueHsg9V#Lm#>teT;X4UfYK56cq3esVhW0sM+Rk35Z%0>1vn_VOYQ{dcww3)f_
UP$;~c$NrHK!ggNR{LO^ZL$GJZ@zu80e`QyZgrP&8b3iCPc1e0^-$E17C0v<pscgQgOht-)sWY$nr*Uc?kg8!Mqlf?+BlCN${d2<
_Gm}y44_OJ>vQxds}2zYd6WD`wmNM{PdHGMJnV~)V1<O+0m|htprTpBiCW#&Nd~wkYV;Z|23p6gOyDBx`%!)azoRIX?+a~Ew#V)7
vt(Os3#i_#7@fnDKttFO7bG)f-;xFm+p@p>6@i5f;5L2p-3MtGvqv%(dL+9Dl4ggd-;D#mKN7_b(gD;IeBfD=Rv6l;X+9MVouWPh
q-gDu1J%E{evE%4aByX*D{iV<=Ef}c6-6!{;IpB>w{>4pK-PEm^QJ_;vMHjj%JQ&VfenC!fb_WvzAKYQEQ*zu+VaU82lixE2&-H`
FyX%1-`CaQ?w%Z+ow^xSfcB7iS(tBis39pq)#eV=ySji==#(-^gE-12fqWoa)!<{{)DFBaPvaUIm1;ApFa7#q)pnhfW<6P-&~eE-
&W<HgyU0!)C0cq)z$Mt;?eEQ@zUqx<z1MHQ@9F(s^~&lKsLdX>#y|e_?%Q7fzXvzV<YxUp$Nqg2Y%p<mT`ZIIc*v*z-M>1+(jis}
*n&x09rBqWop#6mMw|$@aO_3?UVcJgQo@kA!By|iR%Y4ygJkZZ(xJ&-5Gl?kct{rB!>G~L&(Y;PG}-<AW&ho8E1>^YrRNmMO>wt{
n7o27aLA(ihIV35EQbJX`|1-MiE>C0@AlOL*y;*Ad1b2a_&Ag7_h7TzJEbi?+ni>%zNkP)>-ro;I3AZ<W!`~pPOX}|x<Yph=-1WN
(P+E^MyFQ(LZv~6G957jOmb`l>_cDm_-9g)B*^*A&zKyf=Y*8a=Y)>wbGR_?dop}6cUBaX>a1wYcLEiCch!>1JXfWkzI`aRxsx5W
I2n8~>i%=&3=LTZ#uMbF!JJ`J<)B1-0WM`NPS%^^ju1<GCUF)G^yxIrPM_@l{&Mt5cE#f2>RujewMAm)Yq1~V4t0SyS5g*9;}a&V
A^gt+l6{&1c-Ts}MfYOOia4Hl#ST#xgDnKURd%1it>^F4O7b1HtT}1s)}}>EI0(d9;n%raUrxrg5Ni#!W-F466P3-Xt;;Og{2PZ6
lcs`@5v#?*eL6aY;uW;$icQ}S;t3&qRgBBBrUZ9MMmwT$YV!ZOhyS1Bmp|g|yWZ>XKD>bsgk@bk`yL^Dk2%Jhw{JeYdHvH*|3x3(
ejh6e8`GqxSn(vo9kbPS=D9ZJIaTWt+7?{yk8rDEHxfj_EwrnKoHJIxRW(2PV8HlXLcrpc5?o(Dc5t?@9B&v9G9a^|3_*pBk1X-y
7oF+&4QO~glprNO!|76VVB{&aQX6!{+Gc6^g0pjiMk(^b2!71hg*WF4F>j;1?iQuhe$lltm1FC#<$a>}<lDvQV{P88(-F%=bz#Qa
)Ti(i+XAGdG22hkEnsoYl3ixYC+ik)K0yIfqpy(-j26Y6AK_JPU486P(RstLk-)BuTVCrLIB#PAgg?E~1|KtSYbWP7SX(<s;{xM5
MUpl($Uw34#!(d8yBai&)A2*`?8QL6p$WS(*{`cQ&gDrTt_OAK5LBfNs6QTm3Of9{a!uZ9UnkpUSJivrQfp4ybx+I=JSdpc;5~O~
7<KsS(Zob<_s@wuA7w2DVmUe3Mk9_Amj3)~Ve9GY)XAi)7Rx4WRCTq)3v4}=O{?;&KXJ3C1nE_852c#iuFa5aj6u_AEPF;Dq_qtk
Eo3c3m$6`><F<8e^8prSrMblz4P?(4vp08+vGj9v>2`s|-x-QxDoMTm&M|(>t)y3zRXd<cL)`>>2L<-d5(!36?C?wsoY7*C_2nS!
o=uxULU}N~upJSBv=13wmeg@^gn0q?f*QeQ)ejV-Gz!+>@aU}eZC8Q2@^Dvd^$EvNe}f7Ux=|)A^?Bm2DRwPd)6zs;G9Ak<c`U0W
7jE6kTQl^M`6*P}J&KMp?n9xzjRVQZ&6VZ86x?laXrkSZyav1SakZ~pB#D)ux`1=%43Mz~V`wkb_!|r~%MNvYyvpTQ?yg;T+it&O
$I+yX7c%kQH?F4ni>GkkW}u(Qu{xx`p9=r5hm?;zs$G}$*S+SjL!cWH>!rIP$wRC@7P;hJ1PN8Vy@L$#Q}PH^P6$K$5Ju=Nf^ynN
KK*<vX#3<y9~qdYU<h%A36U`zZq}8!=qrh5N;k@aSIQnNI-j(@6HL<c8Z$`idxbe#TL=cJn3>g&Qt^4dO}0MUzR&tz{YFm{{+-q~
#<yBqsoyI%p<Edm=*c&wN%nwb?lvc1ECi}FC4HM45R@%7iq@^541p9AajZw%G%I#+fSyA&3VZ^M_uS1U!5cso@I9m<zwj@z#1c9#
q1S7Gy-$eu2$^%%XfjyNt`tD(Z7KQQDlNeVPEl@X2+1KY_PTWAi2)R0KzoR{HCP*5=!q|6ktxEl;487LbnUoFLSFFeo>*C%*}(-J
PgGCkePrrXw&%#z5p8V<f1S<T&0ndeZqY+Bb4zWFX5As`s>PZ}JVIR&iNwTuNUhmw$nCTy-Ac4s356AFyF+><`K&2D`0h~J8qGQn
weIhhgz0uXx8=~dY6H3~pkDBpl{R;80i5!9Ndu5oQz8B}1&zPpUZz$xM0U~EJi_TG+3~9`-&Bf;Hl}S;c_~b=3Sef;RN8un&W2#o
!mO{EZsqataLWn<1;2K5BA0v%%vQ!E*_yzqf_kLuBk7D;Iv)3J`hBW(+R#_&uUt2-UTsX8Lxqkh4uyo<0N2Qc;qX$Cv{JTq=8b~2
5nL^#(7TD2-7{Ewqh9D=tB*-p<eo;&A#L`xwZV072>*xVWd9%S*pbNJ#@LZ|j*xehdWcX?!<Jsk$<gR(;Uy{zWAgRaf}d!}>2&Mu
t)Sp=Jc#gH=`u$B3<D2pZa?h09Vxxu(6O;F#Bgt<^;2%exnsxYa>;ei!jp!XoX`D~9Aljy!8~M${e&N5L!PqjkcgFL@c9$_ws%76
JZ8_QMMe>Jmv-Q}u9DbPTKE*-TDpjk-blKdfI~%tXx4Uf3T{n@r10<F%K?)20a4@4twM(8u(fsxRboXQo5qm9^Q5;x5Yzw>2Gbdh
fm^f)sQjHI+UU}2bbo)b;gIg68a?Tc!ImX%bFH-W@n{ufAlHV7a@6{H?!c6W{!APk{6OvH%+6tHW#;LyG}1D5C@sRdJ5+#yEFMZ@
uRKv&dL@g}GR*j-1Q|+C+`C1>jAPMqL1AD&F@uHz@3`?P=`@V-waBkw^lhDR!+`May>~!y2|41%6_cUEH`~YPB#zv>97C8ZUVtOU
>#yoa+?uSlojaCYZ62utE1g<%^UuPih(C~%KSd?)Xw9RJ;ZI)8_AA?Fi`Qg7^A>wy0r5hc7W?k%z5eCHyEktkw7=ZG{cvsBl}?Kz
wN9yhg8s@D3~py+rE$>xSY;G8C+1jI;!&3((mpt~A#W-OZ?6{VAzX0cvOt7;hcZLBAe@OgeGU)YSl&c79x7=7W$^ydqn!x4HzMI}
RIW>UPTcKO5CtoS(Ko><q4Tc6gw%@gBpsn&whi61^7NQcPHV@wu*;)xMgch~a8z9@w;TbR>Tks=X&NBHQk)l3_sM8Fb4R5BsPXJ@
IU3y!lbG+y1mAG(lD&929gg&*Jx~UNPb+tGeDSqGF$O3GCfPe3Iyc1z_`^WcM@Z{U@poXgDSj$)h}!n%9u#-?_8d9uI@$R0S0-CH
c2pvVl~3Z_Gvuqv<5M7i%ZnT{5GC*hkSYR&QRG7_e6y)AQSOtw##R#KzyC*Vf57n^O$N3}T~twBs;rh2YR5PV6p7Lxwn>UaiFE#7
PmMZfYs6zwgsIk4s|yCtqmNC|@aj+U!aIhsE4JJ4au9W1m%*eWN)2~%1Hjf5A|<tOMOsn#%^@`j))gH^hlDq9WQ{fEyq;taEy~Q{
kvnQQ40T!ok<}igN%5v#4Yxqq_4TR~sOAwodcnPoh~_EkHAv%s+G+|CqVrc&h^coVi-?urm{gLB2X_0u(mRJ7c}t4&(C&oCvWqy%
S0f<FY?gwOA4NcrsG1#J#S@30k^(@`I_)nWIFiOH-6f_b3NNrHQho{k>`xJ65<HdNF!Bc+q<9nq(d{)U#1K5&*C4CzwvY_44`q^9
J6h(V27dz?{dbxmR?BLViR8Y@fm!9Ws$wr<k|G0{4!^2jqiPk*+d_GI6#zlCAnvJwf0|bVq>m(%ePJ-9vDejyi?)p%I-;vQ*k>IH
cOwwSCH*YHZmJT6tT*oHV%Q&9-)-{NtDBe+nnY8!N0;6g`$(~J>OHVkqO`2zXD<CpT|MS0sXWAcikum+wQK`6cxORf3jM`a*6Imp
!6>GTViON++=)}U0;H^}hr^E3Iw#ZXo-+39o<eumJxnwx51~vPu06uC(0liW=U4*!t4fnj!aiM^Ulu5Pjoc}PD@{dZkN4D$a@L!=
V6u4+3FCVgJ{gQ^*>A6qJ1W(?ULwikiZ5Z2^M`O+AT^i}k^Y^^C*?<aD9U_A&zcq~#E4E2Ke8Bztw%`u5JL>MfSg3?5xE@r%}NCG
hbOwUw48m^%($P3Ql_1}G{v7v{FBw{&#B_lRT6W|2v<BIOuDR6!lQ{v?J5eQa1_+-as-^@Y=(UU`+XY5;WFa!wtxCFY8)5@T}7sL
Q$?F?;d9WwOQJ}`uSpV8pOt;yHUA=)q#%}vV9oX5Hu9Fgb{)}=tkJGz12L+?dRe*H7i7p!d(ynr-g^>8i<b>kg>3;_lmXS5qNN1q
3S~k;s;sRI%C@krHr1h7`9Ph;{_$+MDw}h2=d!yIoK|36lx3*IviE!w2;tA}LH4k#<lA-@4Nuh!WG;8wX6f$nv=CoYU<Jn4mP~4>
K@23G5yjI684AiG+j|j`qTrque!bID9NJeC`svkFJ8>z8EOEBV>qqn&+b4Fr^3f_X9T1~5*>F{MQS-9}ZoFEJNZG2=F*oa|#4D^l
4vH1oMP)KopE~>K1gV&-WGWQ!6w-4X9b6M2b~Uq<i6>Ij9}B?XFI_&z4m=w1vsgp*sfkZ*&=RLFa#(R>llGqMo+j@-CDDy_)Ouhr
qgWdT`C8b5MFC|-QA?EG7PykXCh9Zf_~!E=a@V^u_Eg~F)ULMKGWt37M4ree$?z7^v(soP@EQp4adlF?^NSbzICuRHcmOe(A5Wb~
=TRi4p)am`mZ|pcX9l{9y*pD>mu1!Tq6}U6g-gypV@~!&xX$diIE-#QQZn9(Md6LK*Gd-LtEd6mn4^|Z*s`aMJOH*fQXrrW3rhlE
=^8cinELUMo8x0-KMtfnmA+e+an;S2t}R+j`NUImNYS03zw{y-j+v*mg$(l_XHb%j^5ezNEwWq^`ZApqpFG0mGHLFvd&(sFs6*g+
uA5nmP^KZrF81wS*RHkf*1#?`7Dua6DLtcG7H@uk-6RWi8LCS51ned)?heSF#QGu(hiz6SO;fCk5ND7+!^v$bc-9~{BV<d&Li8P9
Fie*2hHo_2NrWM`cHpXb908s09Oq$|hdpw?Ty2sqoP&n2&6tR1BXM&p#aiJla_p*aX7r(YqG|16w6^$JBXG8hnC<><p<bF)sbv_}
k_PhJO%a>&2~Es*g_JAH>F!c}f&<#<F|Otje4Ff=`)a?+Q@2MnZ}cfCBU&~08&4~5Sla2H2QE`^3sbJVh7L<ER48K2xC*p(832*P
T3KZeL1`$XP{>0iWcig)Uz841K~Y*pqt=K{MXtUK8Y$IjGl_;xq4~yEi?7Sj(EWWGdE=1{^?_^v;)viD4&*)KdwMA_x-Tr%s)L-B
yEf^}@pG0{mN&gJL&$ah4h~naF?@g;wg4ape32iR*M1x#*7|Ye=c+0!Y2MjENj$Nz9Pw7pza_fx)LT+Jtx0DbY_UGaiU^N{2uLUu
f7|+z4lrO>W%tTa7CT)*iq)w@zH9S*BDoWl_PufZB?v}V1WH+CI}*8xfyjM>waIoXTo$L6yb|dHkLQdiG3fMXLh%ZB{yGdkw-{ki
h&1_)jrhT8Uu9JpbV;XQtK5;9rb^OChh-}Abn3RvzCL8c-QPIdV}BkvL0cRUp-Tb$wg&4O*c51A@*3vi(k{mtTWmXV>?@R5Hp%Y2
qB2M@H3~Ka1e$-QvCDYi#?l)_fT%yo_Ob)JI0p>|TH@60?f{QU@bfl_!xB>+_Q5vEc3bUf=~;V=4cI4eRTBS>ur7O0j5AZQm^zE8
SzS!F>CqDr!orpj735IY%R(F=FA^Hh;B{v9giO*}VsN|U;J0u{2Ps+RpTRFXJSH3g6H)`gZ@U7L&<Sdwy0A|jv7%v9DT2r2$q%i*
%11_vIX#xOVUax}C+3xj@k~)LFW|hT@a286BO&;qT8cpjS69WvGy9(k|I#nqFPTpGb<gd_3fHJY#pltjEKO^{^`<ovI}R==I?w<L
`^!~STv`p_bd<IOBzhhhA!nR6T@BT%2J(%@24v6W^`LWwUd5*~H7EFEQXT~75%c)%?T7!ky?smVXTz%)Sjr6XGlKNY;gM*A<UVN(
oOFXDy;eM87P2K8)a#a?qOmK?!Iz-7)jg8+jyg1Q&Jm??vg+a%LXn%<s71r$BOE1u;5g17xlTU)Cg!eBe6%-aix%f2NrRpD67;^g
ZP*Hd9W&Z3N~cny7;d)sazwHrL7b&z*+|m_ibkjARD(T@qHvz{Xi|**qvtqi<OVq@@{>__WV(xx*7fwBBo4Y&)|>5A|8=nok{k)A
(KAW!hx_%f`wb}94qeX<Il`$P`BVUsN<hja&?obrx5CC57xM>Dm)h<fMS<mT9U-vt(4Z_!G;GxCD0N87BJ+9tGGPAGQ<OtNUG1s{
>PhyK13kd9BZ%uh6de%DvhfXu=b5s%q6g+ct55__V8@^<zas8_QIS4;wr#RUS>85>1*tb9ij`P;?#jiUxT#fx;ocdrU5+*zod>k-
_DKoQ-v^{*<2%f&I70Rjuv%!_O1ZLdFr8fLeL>3fcv{8Oy7xK>28f9&r!Lz}%;t(t1z_7luC}7MP@KyCwAlb0_fpdNi^}sSKIKs?
=RfTU97t79ixL_BpY~%UM4uz7)>2hmVF0$e;&E(_u?}3gTsDjllyD9NcL=dN4l70xkkdrSSt7W%hG2vw)C_416+SJah%2kApo(?b
4O4huA|8U#gEgp;zJSz^UD3ep9^|u_|A=t3ps@faRNo(Jcfvw!1`zB+HLGGw(Y}M{KhVoyjDfm|k+*Utb@I=$T&fL~GW3Zx+tk@a
hFz(vnqu#+PJ_!RhL<q3cTb%($AX;4P@}U0I^!!oHH;m|Uvb@IudgD(p%FPivG0hb38K4x9-2tOx~jn{prz1O-1A#?KykD#wz#q+
7VPkg)jIT7Qp4o1zprYP?JWYo^B_&zup7*EQExnqhJAN%FOb~2)^@+UtzwYmQB?lI@o6uf@-UiYWMIm5oqY@Wa})VDLbA;usjjr`
CLJv;Z`e86ujwawj?*a96TjNfnZ=g$BFO>qx<vKz34$1P9aVEJ_BJ;$q0OkTa*$yY^lDvY2Xt)}G0JGml1^w`T_Qvw%)p$4u6r`Q
gp@!DvPTWnls_^-Aq~&-5hondB`C6KOxpr@*be~g;2?1|Go+Y4Zuj>9<x0iYETqI%4ZvTt-GgS2pW?t5OGYwv3q<oKIWOiD7(`@{
>h^6t!T_s7N0!r=TpO?dk?t%6b?f|qNUbR=!?Tn7-fxoMR$c|4H4HKV`aXSh+56Fei)LSBPS2AT1_@yT;A<Yt-;9r4@$H}1hT+Ie
<8-;@=_#U_Y-^T@UFTcy8Eb!y?nHTD`EiJ2c_Jy+hq8ohr`W;;K)x19>9eWJ19S80l4ki8`EdH!9EfsRv$MK<fETGk2tP-8yD>x8
*}Grmr=FZ17Ex>qGzxsd6?^02!iiiqa%7qar7!g!9_nfhC&X!Jjd6Sm=~TM?vVHo}6St;+<Hz_VStk6L)S`K{1%a%hc3f>UpxU9x
tGOb6&t~Ol-hxEPJe74u5S|T^7&vX$`oyDsIvzEJC2k|hr(pjmHkj~k6C*n5VxIZ+`EDMF04+Q|R8l&7&xM%xNR&gB9Xq)pk}A>7
s_&u>k~X#^ii$(dNWT*uKx|~p=H8OVUwZOqYjSBfu+U9)kS0DS_x%WPUe2_v(%!cyVC0fXlAbj)un^D*JTL^9uEQ3xc*vRclJC+g
T}9I8DNKtUT$sJ##gPPRb)BX@F*}ev*sJ1c#t34(a2{;HZyDTZ`}PjU?wUNqul1@tRud#|kNiefH9%n{YCr6S4$sM;*y+;{&%Kz<
M+|~VasB|S4dAyO)XNK!y+axi%knJ!-rpB>zCt=J&cu=2p=GMUx&)cW-&q|FUl{I2ShC85C>$9L0Na6)Y!G8m9!bMcU$9SEAxlZJ
CHeS>(H7$%zw4R~7pMIol{wijoF4t?Z}V0*33zWxTakgoTLkX~a~mq|h7y>yIVzc&loFj9nc0}2+@!rSq<n}n4C8zBv4kg^=9gwM
Yl3HJ!C}T`k^|9{UcMemB9=4wVV+5(Idud<k~laBTP5K}InwhbpiRIJO)#a&3ZKq!^~(T_y$AAHvF{-_EMcPy=YHs-CD<J5yTl0e
e7#am@qMsSL%<W_3Dhl<08Y6Yw<O(n(Y$)!#QZg6KLD+IfD@3Qbtx;cLj6LDNwVX*kH5-Zs=$ftB<B#}3&KzRZFof>k4)I+95gE@
TUuaL=Pj;Xhq`Rxt{@~AZ`dVlF9@BcX^v}6ZNPDjpO|+&BW@Jlk8N$kjtvs9#0vYXyd?}<Y;b2_(qsy1b3~PKS21wq?$)d;?clQx
HmiVUQ4dc5n%@UI?K(y^UrooCS>$1dP8AJR(V-TeqZD>T3ttyofd7fAPJB#Cq~2d?b(fu98!@W4Z8eOjBD+545=Tc?0JrM6D&4Je
bltVh_O9vG$mw5RX;k2@SS@sQ4dpH=YP?zoPJR=kAidU5UaQpNslzPer@1KbJij>Kc6*A=K%D?=^37T5oAx0!BngE`Csgu7a%QJH
12nju0S*1>bKj|~1W8RgNgh%(v*o^`ukeMu6Br9FLBl{UrLwP8-&+KIT~`~6x0lA6s$dfg$L$Yhz>9549K~Ss+~Jb-)C|F0cqy81
L~S9jr$9^*wc}##ys-*VA$1n@B`)SpirsUzDK<#fV71*<LB<_PM~LAR_b$Gt8|~1Gch?FLy`ox?$Cj#z=P2=6Z2xA>!g8#w(&N1;
>b4{1)VHRD2{=@Sy*;IX1AUFO*AAMNTDZuYjseGJYY8n-R3`t<65J)(qvb=BLtZ%YpMg)>%#9`nL`VyyDM}M;Epyccs(b-~Do$OG
-VY%`Zzhj}j0I|Rk}7}<m)fpV-iL7c!aaIQyhl$8_y_>i?d3G4+qiN-pJLe7(HE)J9!W@Tc+!qGx9hNC>JdoVn69WPno<xK;-!NA
Y}GOUh0OsTz0TugW3`3BIbL(_rlw*u=N)%^`BKNKRqQ+!u?76DM#(0C7rt!TKL&?XNA$=q;8y{`g_iUVmP%4QVFWtF$fiM}k@OJt
+$WX&J?txV)0N4WVe-XRm^6bmU<uR3oPzLYGNHUC>~${~4=<zQ{v7rau@oCg1gPnzIyJ=5w=(ATP}$N4>eE_4wgo3PVl*^IodA_0
w~{VH6)LlUfy~WF!6s7;GsiDvRc!y55kHUXYawtXsjFA6kW#%8jaQKwLFkG$C?(n9&oE?DBSbl6K~3sm;Ot{boxnY{<v5kRVe(nz
e6H%q+s$|M^r!5pwn(i`jIen#E9IB%^C@m?R$6sr<+xNLeM9qRA$8l;D_ZJmmsJ}jOH5Q~`|qQmvUN8-m7A<EmSPO5V@B8?19y@Z
QgOvl7udEhr53SwqS}<0)TdTfm0l1|L)G30<pINIq{!le=uau>F@Ho@WKwJ(e`onTaOq4bL(Uy}xUEP=8+;#t(w2#AIOsP{3$o(1
Vf$xu&~UQ4&0)_|t6{eoAK&ARJy)er1%g`P9-dkry-7nzVxJf%-xjH_SaElo%UwKQ&JJ~r`a5%R9qR203hleXo%9O%VB8U0O2Crd
j*Zx9Qq`c+5JA%1?~<&xH9rIV2C@T<PI9DhVa3wPVV=7&I4{CkTAy&ss?2S1pj)UO*aJs(xu8jufJHn)7_ybHnMf5Yme_6m7A`^2
44-c@7YFi2Qc7W`k{t&5Y=HcLbW3uPf*H|$vb_UxMlO$#F!%)FV^Z9xww>F6l6VFOsPU1A`7()*>Da~#>Cy_%8Fr$?0aQ%0CqbO(
s!0j#WHF$K;818V{3lVORxCp_Ys7E*@nly!pTLo3#N2CKcQ#)=<!7{_1z9lgbk^co#FyJR8w*OXydnmIJM1aj|5Vivpr*u+9cBAT
zA4aG?aIR){)ghf)I7q8v^X%WAKag)?e5<BeZfKw_<T$%r{X7G8v}c#vMcNlYRE{<MquM(vJJw4N9*Q67C_{5PSt^(=%gx%pp)aU
CAoF_F;vr$Pw;g09#lj3eDb}qKWTexay6RFG0H8~b-;akC$Zx_Q+=nJ^CrG^@@9o-@BvIc*)e}n*9lLs8x_)#!ILTwdHtEWNqQ6y
D$1J_5)h7tz(3d#Og@2`Zl$s=b>)r-VnbmmJH3Y{&-TQha+Hk;&zd`t3wiG-L4e=+gOQ)iwZc3Y=zzV>7vU^OUhfCV{^-kHib){U
3R2bqyI&fI75YZ=siLMV`qFV=X~j)HABSYdTDu2;H{XRbEzU)`F5P%OXjJMxH4|4duP7vSluqK62|UBFdUT_cfpR`_t`cU$=ee%t
Yp3i?^SZ#2QF@1i6>{(S)i!K-bMukQ4>d&c={hD3;e~}PUp#5_0bpJVj?B^gy`SmJ*>C)cV{G~x92X%HXRfLYo7RlIueY<C@9-M6
Ze<abahZ9T`hXI0#iSxA6H-jXo6Sc8N)Plg!5`bElCC);yOG*hxK3<DDr1`CAg(n^aDePGo1!d>2EYO4&opxoWg*0_Ze|styZ$^m
hoTb;$(-T16lM$p+lgEew(j9Lz538V4H<>rZ?WO}qsXvtp;-iy7Av$8Cx5R{A%Cip8-Hx2tGLl4^&lphk#Z=IzHFBq8Y)Z>#R$GH
3jxu}mG~$aghQ*<1kdzFxO{X3E4?nMD_^w@b~nX~H?mzjw1^I=#v^Ms&{)miVw$133pA-gv0Ybn7HpU%4OrF5_5pd`)JF0GJsytv
dRp6lJ=HiBsVN%Q7^&Y?jc$pfnxo@QWK+3FRaA!>#7_v<2{b=sPZ2$ruQgg$lkc*1Y^~>eBlzj|iHPBU%1p4*Cf1^H`QXDDj*U-o
G6Q_y=(!bCZZ=~}A~IuYZ2Q1{r#CC(?ogu?neG!euSXMS8)R%LvN<Q*xaOFiV~enHGLP$UvPX`$QW2|yY$2GI3W`FS93CTc`r9X!
WdM$(g<I^Q(l5bp$jb))>;5sV3nWER@nkH8ucA>f5@Svv^e0Ay@(v<{)RxG1)Q{eV725+%r9zy}9;ItEpA6@I;i0U75eJ8&X%2y8
6FzZ|BNKu3GQD-#toSM^P`z|F9`}!MRYS)#Q(8RJ<P>MhA)&3^IW0a4g&to<Oa~+pEAs&tPw-~rUIyaA8EeLn0O}$=gj`c~gy*O!
(gL0LnggFv6w+6{-be1jky;Loh!Ph^=C7e?%e_`~+QxHza75C<I9shT4~NUA^S7^sS^I)rl}S^|f4GdK4%l*#lKhDHeAB2TOvu<k
>oY6bX3%F|)ZN}fH$T<{*)Fd&?eL&CBgKimwnQ9Ic*-@kFlxssVEq2!9NCd9wxc)!<0_eERla}Ue$EzxZc#!Y$m?wz9$|pwjl3S^
is0rxx$a2|b=}kQbw@M%U`lIJqMN1!^6rMV&I{3egw(-4A18?^3K((E?7B#%!F6y)*wbgm@fM_hh1yU(gI(o?7zS%@sh=6@0;xI!
3Y&m4ym~x56e#Scd<>YxP-B|x4@sC%>*DU7)aE8NPRfw+&P#ZIEEYvFyU=ChM^2SaxWZ^52!~1Ajz$SRHsTj)jOjIv*z<U8+M%O;
+>Q;pS&EWg^L(7d<<#Gr-Pp2)8@W61M{E<x+1!lVI%G%&@eO0qt)2OJ>H9!+U5D0z1{<TF`>jNOh_ESz7FVcPkEVayw&NL{O-<c^
q@mw_tTLW}&1vvWng<^hvwY_vLigvymk4|oPv8(Xvg4>X#geXz9h8li`)Us<!qO>0D%Hvfl-&rj%Rtko>!hvXHJA!YR``{hpzR&b
PvJTxt>`TAbm`ipVfr=*atA87UWf`qd=q}tgTfIFrU|x5T1t0zs(lj+7~3myK5PKk`m_9aT0AW?)0V?20V!^hTw?_#@t%~nu#ZU#
mm{~0X+hKkPum@2U9|9>vkk_*x&vDgBVti$6XhyackOf5tvIV^HJ=+x6ozd+=|d&(Lcp*rw9c93WsN{b7ev8lSuF*#{{GZ4uoYK7
T9ASI+4OlV(#$*0h{)9P3({o2>Wybqxn*r%^~&lKsO45_gC9{{jsAZRZkEZ-`hSl7`zF|6;_kXwCh75zPyO4L0mw&r<_cKJkFSM|
m9SN@_H=dXRw{O-WR9$D7I~A3?IN}38%A!aH|mxmb}1y0#}s{C6hb{{7`?JYM6$Cz`Gt<$gKG#R77CZ9l1FpHhhiS28IZC_8ox0v
q_fdNBiMG}jN3wJ+)5pO9k`f&?h4#zJ&hiIUC9edSA$tvxK&wNs_PzZ>81>x)A+vF5&8%bLb6_q9Co&R?bCto7<6d%v5n7?#FHob
q9N&*%<|&U@SXcxS%8(8+!_s>-x_Ok&TWDEi$6g&Fc`#QBP*j|k{63(ksneFPJ(K70A%+oktZ>Zs=PTZXyoVyG}xtBx3)PP%uXYv
=S3yvtU7$4_Q*G;+Lk1R7u*LTY31r;Rb;?=6-=>U(?tWYk8<3yp&oy)w#O&Qc!rDMIE*v<n(LtKGg(~j5bPnM#-{%(>b|7iGiAP0
82g?HWoym?8|crlT6csdhZ9fGM6#3<`qP=CTkXp0=i8v@3Rl{1#oynE)&<+#eNXDlG|rNNNWn~;dH>^|ZP>S@hmoONfI-;zmTg@~
umnFgl$1H6>B4*&u1vE}Xt}<svKQC3L*{G7F7ViIei%{m8FC^}=RyWe+cZ6;b%fuOYk&j@9~du*!J%LG*vk;1v$0`9kDzGU*>@jT
ecHMyHzrV>CT5NCaW{?Jj)v;}iLYg`ZJ;`qksBXlgCQb^q=o0*>ZY3?YQ&@ZX7zyIL{(F3_IB)Dt)1tPw8&RkvP*C#-~u$Zo&Dec
(+@dd@wPObwCV3_@G4PPv~LDCT*i5x95%=Sy~Wk3%)JOTe&HuoiEjkh7~9VgnUoo`<av{6>ZPleIE2!pCp#waNETt%6h7}835PA?
xl+6R>CdQN{K#U`sh3ql(l|<mXVS9D9vmLXc2qJ);EpBY5p@{72OChMm<hol;!)`ALxk@ukEIw5_;oZTBAZnuMPduw3Om%4Tj|vm
7LOz*)b<vhY8M0h!P&lmniN)9l*=w?1tsw-b_N$uteym1$k}8%`Yy<rnM;7wHtbTs28haWaBl>6o`JBeb=UQ=r*6^v!xo#OY4kQB
mjb_!Z4Q;tqC8y%jq#17b0h+WRy3OS4ke-Xr*Q~RFNNh*S7;lL&J^S6q#thW(Y^j02v1@cpr7}&0+b?o%EB(v)IIIDxbFOI_r9l}
m7aoaIS(SnE)y9L7&u;t5A7NL!>6~HTUS<E<VRS(QEi3$QCh5R7Ql$%2O|GmRo9*^%OGN|AeI1*3JF=)(dJ7EFDwDd7ho14qql=_
CnMcQlP`VKF%-p)HY<7=82AY6rWKv8g*dB!j&R4{A$Pu*zut3T)EcWfF$vj4**;>VG>ffPiPe*zlET5AkK^%C(gTm}4AG}@f|#qo
=d=tzwcDO4K%9>4j)ZuecrG}zneD{`czof;MuJl8`LkHT!jM(zj~uOHTmJ|H4y}eJn{e=H|Ee7dr~O8*wiaQ((FW1ZexnYI5e*#|
`)RenKRNre%^9jjVbOiXlw9*qzwY7P<J|Sy5TmxY-?v+@5$dqvC%eMw5I{rNemx-5svFZL*%oUQkLL{sf^wmL#JT;VD=X>aaUG8b
dNB)}VR9wR8d3Igk*U9BM<g%~b!-xx2!$~(Albn6^XP>9BlVPw_N;jAJiPh-aBftETvzG@(Cw-|bcD`X!MUZkm>8+tC8+{PykN<+
5OydsjiTy7cgPeg>WbHUT5qX7Y`65E&A!_0SQ)0G!L%loV!{}aq5y|UEVr)Bya9QUtPl>G;Q$4BZu1g&w{ygfr#HTuNY`5-Gq67r
E`3A+iYU0sdIr8vM$?)5I(uK$$Ug|q!@VU<^+_>q8YS{syA;0{B&n^T5{f3K@6Xl<9Rgun!M;Vti^8PUZyt3{Y3;jug+mIIr~A@O
82FR?ctVGKvRd{{_Bm}H(=*yUc95XgRlRXJ=v~Mf(IB$w3=y3n;sS;U8Lqa)ZO{$5E47Zv!sTY%xLzyQ^)+67-D6DIt>I8GXSRE|
MyenX54h_pE+qx$3g!2r^=EhGV_QtEtt<_x`@{__2T05HKEZ{aqKqvR@mb6*?9veDQ#kR45NTVO-?*Dxz<3g<?O}D1c~-8$z7q#Y
d-1GH0%S;h?Oo;o-oR}G5D}nW-)E))xDUv`!86Vl7cs%dE_t;KJ<%Kdy}iQ;cFN)oopqj_w>4S>Hfbxa7}2km7J<CD?pY?rMrs^n
=fZt17j9#f^ngqH_zIn+WX~b-;!kkmM<m3O!Qerdypi^LUl$wk);loUrcuXeqaJ;@);4hDaNFT#HY{|QwyypWAK?!#NL$<5EFY-$
I1X4#_j+C-S#o<KYNxN{u;_IbqXI{0(Fx?hEjq?iqB$K$@u1HZji>XTiA79W3f6MapS@8I%6$@OldK^j+B4hW&@>Ms(!h;`ymupr
ssyan2x}9*@VT@X3qh^q)n!hc(j(%a%3>>#*>RI>4@qgMDt_e2%~`=xrGMlq<D>ql1Ybc9wdnHRPiMo4GRr)rRppt{X>11Po^rhF
$+ze|HL>TJ%fYIepgDF0g-Cy|BR1$CAA?r_GejYxdfFcgrT63+e*MdbcW>T8z<;@Y`{CN?-Xe>h(bt#w%4i%}3D)ShCtlWDGx?La
aWi}2u-Eq8z^3|JaV)kh!Q#MuP7xloE)ewAZ<QuZVZV9QVsl*cY(JD4jcv8md<zb;s?H-A$Hh#R#t`+*Xlx<sDr9s-e^mFOYm;WL
ya+MpG7(yq){32(5iFFzgHc3rcYLWx-7SlKk<dq&Dz1(ir)Odq-zBt#M?*J1)(DC_;{He~WFc=41&YDgDBqx74CoHEhN@KKPxgtL
$|Cux9gMt2F#OK15%16pds7_5n$k`+U$FP7>s0gmc4cyr+nL>&0%$(3OOtbO5+yVKBc5P{4b)VKMuq&k??~v@_OMCGztXBIL9)d$
Yw&41?9wJ-)^qU$W52I(!1VgCNA4Re$pt)KBd-bO4wSe2=78N+lOc96S|T37pzJPJ7WGcno}iux>W{&kt{#o6O`WX>P8rk-F^|is
XA-&#f%>*lhMHV?*{!;b96^3e0%MX6mX~{MjxznmW;y;Wm$1JJW-L`gk!jbnVcCvYsU*UaAIJ|Ku3QEvExWBjJNERpWR8rCtk$Vh
Xd5^9!)0S-^I}QHYMDv$Ovy^h0hojsp9Iy0%9e*!UnCRh5gAx*$e%g^t8;G6%L6gN{QiiyWY-v?jq<v?QM!todS~WUli;*Pu;g4d
`&gTIixasxU9#2ZvJ%JVTD7ig+f=JIAT2KtwOvT=EOjj*LJ6Qfe~-F=#JF6t(mH>a7TESdSOvGaTIZ49-g)SR$@5@gemb#u>R5<H
c<z|{o_q3G`Z>CEtJQL~P8B>38aPe>*@2xRh(dmQ!NY63W1j)axF*kw27-HB`J)2^)j2Qj+jrh0DJmYRERgUKBqv~xhoI|3t<z8E
B=|I}qdQ1{o>_lp{+Zh7Oa|z1(G(Qma?lhlZ0?y7NvY16yjuhTN8}yk0j3e~j*(3vnRI+%FyxxHtv1y`IHKCmJ@}$%wLyhy;8>qw
!frBA9Ung{M>)zpnfA1h7Q3W0-$cOEX^`(9^>GxWmRk@P5)k^W_ll7^p0x0iJg)L;WEo0c{XpvqH%GA*F*wd5^T>V7JH9RC`AJ#)
ou=>dH|7ODwfqW78&r<c>q8Ee{M}(sT?IeV;C0q`IvhEDQfNmcrjotg`SerrQ47RMkRRj<dFUubGw+eFPN@{iL}_60qHc<^ELi17
sX;(dKg}!E8WKhW63b+JcS!CW39kQs-~Y^)ws8zz--auloP-0m^7zm8O3x-dx2%Vo`anFySbLgg0;N<1xt?1>1VExTfMl&_If>8b
MJq?Ys+VJoFKEXu{KdpWj<!MZa#Cyi;>fUNhT?`Uimv#^te^6X1(CQZzX#`!oSa(!z$iu;chokj`kHBNFkdB!2SeP|*p_+&mcP)+
=*-5Dd1epIVdMT#uuf3%<!1j_w$K{w>1&H-Md1Y!`DlK%j^LVY1-Yy*H$(|ncH2ZWjLTzmnp)Z%cDo8^=wS;Pm10QE&m%UUlSMH~
UUBMUW-|8s73NC?=tlriNt5e!7P7^h@8byZu8$+we^(wFTc#Z6&Uc4lw^3aDUaNj8rd9l6mIbBou44BZX&l^Z`#RY+SzXww&<8hv
+z#B;MO7F3$0x7c5X@D42BG5&3!zY?T3!r*5~$6AUiTFGF=-ZOk_}{aa+P%Vr`kb){TZ8L`8m#j;ZG0cPyX~o`J3#MDHTKad!n~E
{G4mXeVOmW<?Z1VVS(wIW=2LVBYB$y`^+2Dzf`_Zp=YbG>r|mT5$Y4~@I^Gh$jiY`@Zyo(74Z{DA)=&4OytmtLfurw)OJ^qMI~hj
)LQGu7nbrvA&oM$iBU?J0<I;^(PtsP=RqL8B_Zlxz;)Xdm0Hz#u}w;QSZlGD)B}UX9_rFuk-XX@lJ~dCOt!^t2P_hOcSzx}|K7~l
$k`aF2O1gh92U7`Qk11L=sZ%unu~$@uh0ZZ9>`d-!e@9f-@N_#_WcJm?eBUok`ipvB4fJai{8r@^q;HkVe{e&PUla*!12B84+fJ(
-{?+5q)FYf7e)T!`URdH5myhNND1iRFU0cUU*wn$|G^4tqGwPu%MT@qKf=F}4yOG=k_Y}`K@et5F8C6T>mI9Dt7e<*n)`|aVQaUp
D>;iB8W%Eyp~*`2@E6YF#($xIV50>5H_B^-)(8QE-wm<@Rto4oMvpf|vqrYfyE?()n3zwZ=4ArE6-6XTKX8&cX<!(iQ0mYdwRA=$
712`O>Y3kcFWC)>@=o%rkXC$n-5(AH!~S(2)eAyts_?~0Z(aO`qEh$+_>b33k^JwUQT+UUQo!H;_$T{Ma^fEd0xJ9z*rvJbU*k=N
|1)5J4dDQ#=l#)OG`krrZbp+2gTXiWfBzr;Z{Pn8EYjgLpDu64%f;emG9QDRWH`=l^3i;rCG&YY&4zRU{n=!^Tu+CC8!(uUZzkC|
zggtd;mvR`%g5OuodYn1Ak)PNq=UioCSOdaH<M%zZkBnT-=w2_K3LAD5cHtiRezveHPUz09wj<Pa!~9mITOBnYo#atx#ZRo5cq#|
qIYOJbyb!JT(A9wrUk*t7B)vrHY8p1{=TjbclX>Xi)j3?*(CKNzUh#DJ``L0mKd?t$Qh}I=W<Ms>~hINLy~aJhw#`sA5Ly&`DBi^
d3uv9M&p~=V6YfZ=d&d|$Mmm<@c&38V~>0mns^*V3=zS9=P6`9`E<0N&!=EG7(pg6%knuG4$~!K9gsUgzLF(bHi0~4y_ha$lhGKW
9WMr8GRqJKu?o`<qtQ2m$v2a^xkARf-_>el)5QYhNqz&yh&M0t;Y~VO&u*r0UB;sXOeX`p8Y7<I(Y_vo=`fvT<76^hfNVOO42Fw5
pMrcn7{lpLz+g5S4yVh-G=<!KHq60nl)xDVgXLt94M*vduZIv1bn;{_B{3!Vm3WG=SxtQe|MLK9ef4mhX+v@a$xBCa+iJf>PnK3k
YT==S!<M}y51A+s&$Sz&C)$>MJX0+N`*Z+B_j*0J84c#^o5^~yyh)bJ>CIv>U(CRII$I`m$Fu(%Aa>f(!{?-jIL5%UzP8f{!8o5S
X5%qzn`DsKMu-^F(ai*Mi);zj*?2Y~6fvUz_e2yyX0HYrfQp?U3CK{PMxwZF!>6P%vQ{D@Fw<KrDvi93@@$hhZhg9kk?Jo>gp1Mj
sdT)#FHnN}NBDa=noMV4w!WFA6F8Ls0$8Nu$<2H@fL&*cCER`e$SE|`lIK5#qa&t}Qm-_LkdqA|102FBNRpdLnoMpM6Uem^_)iFA
oUWJ4^G%|-_vZ7a9#!m9B%<>X^Fd_5858;(sDK0c^aJNVw>^Nf{J_pa<OzY5B{gc5!xKbCnHo((sHh!0XDoWp-45|P`3cWoXg;K0
z@P4t$FfRtc2eTcdES0<;z2bf;J+X@J0OfiC&C|bx50U8Xn&HzW?!k3M=~FfBN{zAk{8l%Vh2KvMfxL+@LYuIGDPw-A)7_|OBOrq
k+Nm%8|kozbAwzkH0=*v*w(c0Hb3AB5X49)J-UPR@+o;F35IQU4SMv_nGMVOJ;Dbx!qw?vjr_0Jrl`P~yxlA(8~MCMfTOwV5yfBZ
Q+oa)?Jyl%fF5njh-93R8WLI1b^=lQhz9_6C~u1U@YgYoj%hq+rZM77!xXPvT~s*6`4|T~qh5-2M5+t1%JfsS4w<@2aF=BCTmvNI
sX>9fcpRbtL=OesIuWOcxT9YzYlUp?8$?>)V3zX@kv-=-6<mCF;*LSF)Wc_oZh~)y=bayt`Z8%6AW0rle2W~PGqBqu=F08kk`D+L
H1GflKCSfUaKa#)B_7MzcD><Ziu-=Re}5`+-25ICclW5_x=uELpKg;agafBoHtgqsSnR1d+EjSi_Q@UR_6N-3n+$A|x~TZ$Dyx~v
4VS8Jiv1&5eVWl3BTUN2rr^oK&=(flEs%tr#NyXQ-JmR`U@M7K>7gW$Y3JsUvT0!8@B+wfc1iXi1U!Pn4FQdq8VQZKS<rqS8b+!(
Vb}H!PCNO|m}>zEgWdQqHQ=@tX(OayJem$>h)v8OzZ(sPkd02Vn{)`K>v_IN(rkv<;)#E_`2kJj|K5XU|MhOmjQM^1&pxKc|Be4W
99=Kq{~>b}a(B*)oLD;8Z2<mH|9bHC6#oAJ|I^^fOsrubC*KI148gvq2@k{dLB!6MsZCZLS-v{dgt+ga&B-=pl2^MBwOA+s*+1fF
Ks|U2uZLeBKe2HJOnuJq2=F{fo0h7?Vn(!}9jYp-F+t(I89#$`Li+E@y!SIP(h^(h(UT)dhT-TO6Ngym9$ksDNI_0(GSgtE7g?7V
l_u~JL%6Za+`gTCbG;l6C)o(<BctUIe!*;cvzWrq$f2;E4HjUT46<lVtpDq;LXE}B|90PFCnf3j|LE0lPGts(>LXHWsWUj9Q&qdy
4oK46$cox<4Rx|Xnl47uC0xBxHdxL_$p}o=BQTi22^m4vY%xgl>3WdmqggguPe)lY7*EH;<rJCu|M4Ol4WV>TazxyCWb5^dZ(f)h
#*BQGu1^tp_00$g?hG&GDlbsd0|+FY%(8SmU5^GSL_g271fI%=Gq9L~bUK|+vSc(HgXwrS8V%Otbeaw(Q#g=hF`N&F(;*_1{IDxg
NiZfDzZt-DaIxpHauCZJe#vMwS|+n}Jf9|u$#OJbPV@O>He64V!C*8_)}w5`#Cib|?(kFtyJe&K4PA(vk7G>FBxl18<-<m4KQS`q
3Gm{Zf4s=k6;++!w?VM;;dr`SPSV9>I2;U<*>XCZPg0N$C;523%$DOJB+2Oveo-<Vk0$FmoPvCwq;OuQ=-X1FCVev+j)wEm0x4!2
fdP!+tW74P<zk%;v*Bc!tkd-*TP&Beb-El)M)L$rhF~(AEYsx}PUd6@M>9<4i|Hs`Os6j>Jd6T<$tT2v&IlaCBf$U3lp8clD1GR5
1?3IBBcWyw_@=HtNy~V?u|V`BXLgd2TcRh%rI-&ch^^U!-Gxz7J^6ANpBj_*;vZ`3puWT2zlbkDf%SQZgR7%_#sPdD<d?&$U4uO_
TFC~iVj4-9mS6#8;cz_8M%i$gtpR*IP8Rtx&la<6xrE{x<XJWX!|@D2$u^h)C`}j3;b<~R2gw3ss<wN$fDrr!su=mjKG0)}xgh4x
wv|8A1`cmqnZCwLNJm33oDIjrbOKKT*<d(Ir%(%9q?2(r1Y<OkEYGGBsD$NdKAR&CplrHKl4YKoDDlzKhcO)w()Dn;9zgwPGMqt@
PN0gELG@|6T!RHfH5x8w$t=$X<9t1YdekUg&L(LBW~0dx3=u}fjP<nhNl-~vsLLR{0Hs*$2A4oc%yk#h0_iDVMKY9-%m)K>PFM~A
RL&vQFV}N;7G6&vzfIF@F$U}9WRxrxki*Wi@i-sBqf-WkbI4w&P|eQ~5^*9<vWIn1mX}b;%wxFwBjM2xtOv_91@rL;?rShw3^R}?
GdPavVl>ZZ({z&N$p{LUVUmIQBApG=5g5a-9l(Q(f)5>q;0bJhu@7W1Wu#`ZXDMg%@oce%XZ~yk;0bd$%P09XAC1#Rnhz%cWb14_
p3UZiNiv*GCIfgBgwk`Irzt#q&HzHX$#yGz(N|w)Lm+%OZVV?Ad1_>&QaQGGKmC^hR5=7yN4o%~3c2<n>kK;<U1I47TI_$n;)Z3&
*67{B=fHlUZ_aCwK&<7VL7s{9M(A_kz9>!-Ig29ccUAH9qD#Roj3*?5^fMu+5qF;j`9-l_K!EG`kc_(wO2nCSY}vekj%nazsTD4a
_fq&S%KjNPbg<tMTj#3SgH7}K&|hxnV1CTYuwD=+Rxd7dev#1n6#UU(IGwFWgB+|!!*x2IXW4Q+nU5#y^(aTqH}GJe&8CZGk`Lkk
!}CizOhy@0iL-p1&X*FZ%dv#bVb4U`MXXU2Z4J^xQRbK2Ck!P@O;j~Wd117d+mdi46L|c+=+t72mnNZgNvqdiZo4#~T+5g*st6j$
FGf-p%oa^aW`>Iqm@Gz+8x95&kj$6MG+7Q7%QaXoM#I@K&xgriJ{rxz9Np0-$nH5Dg6TY4rqkhQw(uao<iQe~1ujL`IGvi5yXJ}U
?Lf6FeQTI!qxEb)OO~T)o{uy5CrM|?WHOj7CzD}1S!5ZQWa~5q!(@_9@^L;H!j&G3M)@*XEK`C6yQ3(je|$!=#RC2he#|HxuhZcG
ipw-fvTQJ!C;1|q4wpIn5GXYB*<uW`d^QKOd^VaSU_O|E^-xB6kvk5GOfE$4`@Z3FF;Ay6Fb5z>7wHtt0DQE7dck^;OcrZ2h{-%%
ucx!|bP4y!cmUuR=gBx<&exN*gpz0K<5|uw^QpuP2=@C6=<~GZrjO4+f*8;8EX`(+00;RrfoK=Wcrk;}vt*DCmV+GfmUNKJma}}3
F7owyfMTcfWCWQH#zF!qh_eDKcKOs_CgW^Kz`&(&9`a&;8O@nuL?(H339!vsD9K%&d=~5|BiaSD1&W#0DaJ*fFN6}uk(8G?j!~4<
qRp}+UIyv5g6Ik&C{aP=wEFEbh;0`jXGg`#n)w*t9E_$=$(pX0lY9u}?rc4sCixhQKsH>U`=#k(1ci2*!jtBDozG{}Y&jpTCm^3J
ra8q^p+tu)^!=%qP8O1%0pkVsJ&AY&>ortWFZ6|uy6YE0Yt&PJV!W0@lAb*>15j1WC-CSooUO;nWDE&^2C-(7!8Dt~Z84i?W2j?I
vf*?+7=rnF2LBq40YF;UWG*mTD~EY7Pv=nK80FK&I7xH3^PuJmwbaFWFiQq&_(@<g$p_;Esv@)DG|iSXkmQ4DIz%3~>x8JzYAF~G
vUEI7zz|(0kJs4%;>;)GEXmUmRFRQ4?{EV3$-xpCr{;rnJR79b(R_*m!Y1hujMf~P4+&g|6zW%tVKN5uIb6GJ43{aNL(O%T=XnZ0
cmbb+F;r}mWH^FTp5)W{5}ogs>tsxj=sDY;oLnsFgfEHnV#>Liq4*NWE_YDXvQ%CU+a+$={%OrU1v$uxQ3O&fvXa>5o54R@g(@21
5qJq$v<b3V$blnD5Ww||y!u4tMD+l0Mjw{JFHhxPpORAk+1zhjDY?|d$a;JVY|B!rsu5AUGC{$>QUQ*BY=8pFYLI8sWe(Z$d<jr~
gY|MYg5n{8TGe<wUS#8RG=gsz<0)K~DP+O(`Cz;rrOUw-^7{qKcyMZ90*gjKaWp5=+&6=B0XaR=MGM6{DX8{$K&hkaezJoqWJjuh
5K>j$>UbnL{sk$95^~|pP{?z3($uDH;`Sh0MCCgA^Kk|bjp_U*U1#YHJWMWb(#djolY?|U%pkdq^YK|`&^LJ_`E8E{x3!v7+1U-w
q=*fdVoQ|-D}jJfDW|G65Ze|xO5+x304d46gRu3VIz8}lVWi5<Hm5!%MEQ{Z4IO5!vq>|lP$(pEgUs-Fsu-#4n|N4kP(7T4hQ`bJ
04S-mAuyH=^%=MjkSEgQfgd4AlKhwd2q)mhi(G8cN3lmd!7v>Ma;#mkJ&4=m(bXH)0jj<T8wLsykzi1?^Kk0oM>cTjiI_g=p)`{e
o)p`~P-APYyuZOGJ~oLK=`uMemA<tm43-9D?=3a3hnw?UD?=%R6{0El=mACXCg@)o`TsEqGY7w+2-0FNMfB?N{-*@!_vxh}qA-NF
gbID@T)`-94x;RvkE5Gk4+)xqn~(eh;W`!T0?X%Pa?CikA~sU-ya93oOSR5~yf2dd9&FJ4B+YwGe^V~z+zEz)Ld}6VV+BhiCAlxq
j7*0;6gt3^Jx8!hk(roeTcK=|RmM9<z`f<2$aq}}y6bN%;>X*>P}eEU)^(T87M)0;3>A#bl{}?(89uNJ&Pe21q6%xh9V01P%k*bv
wpNrbM1I0zys5=hJm8sE<%cYr3LM+&@$gWru+^15pcnSYD+4_#?(WI;3+#2b#k#vA4@3*1?bj#%>8rT_)!ct9X)paT2xL1sogh^o
hY%^~)hErAd7=u_3u-E2=_N>ACCpWD1s?EQ236U;c%ZDe{{SVbQ60UE`xF0Q4n-HT*;b#*!zSr7p+S=n<2F2-&9O;BPFQ5><)J9V
QsP08KBZ_@a3POVi%ku<Dsw4o$0;d$;-stj*%rSOI0-U+=+Rq@e&sxWxaUgAO1(=^hNsq9QX=*E$Yd&U`FV$0s^^murD%RNj8W((
11ueoR)X5GznQc+|B-7b=RACXyKvjX7~wA|Ys}NvW^v4x>KLT#!USIv(qP>hVBUJbvY-44oMBjUhK!{<kKdis0s7x*3ic_x(&D<L
%nY;Dkz=5m(mEoAnvXBxs^irbo7noFhbO5HRLX}-<#SH`P`!f7XZTX-AS31SIzP{h=W&K)H3n{V1>=?xn%I^8`1L^T6?IhtTb^HG
>)3#O0zWXZWR6n!8-ZY3*C9I7V)R(0KuFN)(6nARP;|qe_n<5*R$M2rg}<CnAF-U-mI)ZjmbTc>Ka!}eE_-l-l(~_!GDBgc=6!Y~
|D(DXb^ylar3Oj@nly<xK^Dy(a6=j9Ve~c|3)r&y<fxs+Z3NgbAgvpA++bNPLz0FlQ9iYsd}+L$l&_?88YeR+|AYJwPYcdwET#n|
x;@m0sUSHIIVQ^?nUz(8YzLz!vL5)2I30<ck3_UBFA<8o=Y_F7hEixg!4>OzIg)u&Gk(a;mgk0)2#>a)lqP3*SC8{TbTbl2Wpj3n
nsRh4I|-w`v$KSxAQp{E$Z>seEpBR_s<NEV`<P2rBf0T0OGMaB!33_&E{y6Ndd<WofQLGMeL~N>s9+LF-AvPClx4G`hhGw{f>N{M
RGG}o=e60W^x-leA5ShuM)5GEglVNuCRQzwIJ_Q5?D-rECZwL8mKy&ogs3{B+gO52_K@oaVn(GxLtIjw!JO(`fCs9}4WG&aYIT}N
Rw8LusN|JWlT#tTE8q`mfW`@m$wni|W0|=WQcnGdQ&q@2Q!v$JhwP<r{&uJ!Cs7zg{S3Uh>@R7FQZAtF+b=H~R>h25bux6xp4C?i
<o4xj^MjDTUk|><WQqU#=U3ye=lEaflZBxEhO2S)VY@AG7H}!scZ5YW>Mqc+q_+3~M)S%$vJ2omuv@jWuw)F7wIVE(gh{{yd0w(C
^QSXvdwrU=thm8hT0bKf?#oMpP5?<wtm`4(jOMn_7%jW`-QvE}HhlB*vS!u$M`u2GmP2gV^wUt(@D>InW!ASU7nU#h&wsw=Q7>X9
0{P3O+Y92D56a@MNK4S#(iB<m4ksrkw0BZUc>UX*VI0zR9C|@{ltWOfw1I!nM%b%xMeVABYTPV={UGJ7j}q%i@wJw@-`vLVm`3WY
RFJQs_xIKQq>F!<LXT1jY!N2t054db77_B4cSey_VaMrmJz6XQq+5{RO!91gvskB-oAr3S$Y<+OKATQH&z+GkdWO%cGL^K=Q_biX
GF8$MQL3M7VTaP|^AvN4oS;a`#QahiXwF`|frc${%}&46wiXI8>xn$cuKbP`ub?GF21eoCwqO3;``gzaZhQa!=KY6XUjNkl@cP@I
ZhKPx>eYJR^nUsI&D$S)@L$uHU-jT0YKXs<f4u5Tz52>4xH&%+`Kx}B)92V9zW6(M)kmHmb+YMyr7eOsf|uHFY2v6ene$s@cU0@g
us_O2u|L8MRejD>;A<O*SA7|2-`d8Q6ZXe4H|&)*NBWn&)E{_f>CM~kZ~wDbz>|Tshy#*VNYy8l+k5x6XK&YgNg7fe@wz8yJsuY~
AwR|-6s?cLMh_Yhn52{0b&ns+LRjVc6qad}U$>k+wHk%|TSlO^f+*ygnDHoh2@33|iGFF&d4t|d;>mg4!;gyG3m7f8VvLmwW~wOg
VmsSLW0B;(2foxQ8nS+%^?=u#w#C7Oau@|`8Nz@(Q%z{-DWFd8kXY!?^f3gDt`3!MCS3NOQMwR=0mww>SRt#00hPSF?qQP#B0?*)
aODj#8R51XR$)lF?n!nO?=aM26f3jntU404xxC`6y$zcrF~yu=z~YU;9E^Io0m#TgMGlM1XV<_W8L=RP;HS*%9_w-4Yk`LO1GhnB
^C6Bv>NGzYn4K-7F(?af2OYWAWLD9^-+(11Tq=Pa0H@5I6{>x9<1x^cvVoQtu?0Zl^<jso_>=^pI;yLnS=naAPFyEqM2^;YifHqo
0QOW?v67*?Ii#I1F?#tSS3Gq@?YSWb6&M34F%sB%W`}lLP%bgD@>fYk_~hm?B3t{Tu*sr50+~m?E4a8HtO-|kkqv>-DhHduv<4w~
RGv$Uf(nk&lUG47OShmingAxh>AfV+V1qbFyyC74o?MdG2@>xkO?eS-ocqQJk1)fssvZtI0ZXp&qB2Zo!@cfND@6b@fw3|zur6d~
OD6_W7zVGf%rVWXF>I}ZeA4VHjhq~+ESp$G<?PTFpE*-5M3FN^COR-vLY(_&%9To-p6d`5A{3#_<MJtwP_F$m9I3<ER**g_kZfFR
5%2erN<fa)ATJ|Vz+UdhGH`pt$nhHWeN;j<USkI9q)W4UkF=!bOEyL?Bq`N}@8jimk>9RX#i0dxFexZ6ke*hPBn37gO0^pn{LO`v
`WkCN{Cyaja#i1eJ7mwbLDjDG;ie#MS8&z9Z&o}!lfcSy_eJ*uYoZt?Jj-u~D(?UXP|r3n%kE%#M-p-?JJ+bpLq#KN>L3DteH}8V
5*AlOC@b7YB1ah=M$h;n`_YX=B4}~v-2x3U#dzUUYmJn9gK<_j9<@f!$`h+Ep;kL7M4<ON0kcMPafriI=)`MFF%X5@*6}p_tv=n-
FB-d5VC$%&q_n>b5WYe%P#Dd6FLBije2(VtTA*v#sNJZrMUqsb!saQekGOfP6-{;6?+$w}7Sd!zKPUv7RmS3_viz&U+pAv_ze2Pm
KxkcPvm@8Vuce7Lv9jJ)lZeF+SoBV%9kQ8_hGL?kacGAw2ZH$Xmr-$ue}%RlqJ=vCAXPbKUmID+f#{>+FoNSyEAP;KE`d_tb1kSX
TthnQaG7k3f-6_;p|#u$>NOfldnAU6$(?GPyv1#g6tgZo77^n_6Ba8vmVhiZFtrn*9&9n6)k<IzT*6MF8k$rTim^m>b(AkBqU=#p
rX7!V&ziK*?k!L)lcoa=PZWVEnMzG)TCxtmoboW<BiXG{MT(^yH8!`=5;71iqe$E?H&JfEC!L*Iu(4L&88?AfWgY>XwQ336^m6db
mgOiCGha%@EkkxGZ({_Q3Qj1yA;`MM8;^<C?IKnr3ku#s*xTBGMMFdPLB-w=n|%Uly9y~hc0@$Uj6)=4eQSq^a!NlIjh%+!W8G7P
=2@T<&C`q*jD+pfYgPDdG+VhQn!b<acao|>w_IS@O5w8=wLh4^#r_p03O|$9LCYEUeejjc{C)&V{WQwAn&to$@Jb_TdT>iDk>gR=
WsZTPwfM0WMhl2`TbGuiK7iFC`a?*=s(=+|Q&#&RmL|Ig`2iXFQ1kFP&6+80MRTxuurcGJlCAB!rzC-EiM8!onwMTjtfu#hEgErn
MV0Jpfe%&Mw){R99?NGr(X{>=SuHi$xsJz{aW0O%t7uIwtYPw-iS;skeh&wuc{~3G6B{!|$n$05kWCi;88Q$UlEj1_Aw&$Y`=ogY
3>q~F440&<bL|82HkR_$gk^4+a;uYJ%lpX<+Ov~1$XIn!+(UGz{2Uzc6tQI_3rvdAw&m2ANxlNe%nqkxlne?UEM9YgEo)ccOO&?J
A7RVCv~nj%!uV``6gty7MdKwMJvdbFycTdtA{>cPI1#XvIU$x|OS-diCY~`+vM%zxX?>2ss_=mjIv+c*;*mt01Grbl*@2ReCNWwb
$V$p0agtZ_07WBZA<)Rc=RwMYqE&Zc<~Zd+Csn&NBRnjP+QoU;u&{(10W{Jl#h@^=`%qso34tpEkb=zZVith1@@M9W8iyamNn_GN
M_LTUA5IcK0$Yeli(y2BowOc{g$=bBN|-O{>5cLOaG+wzkJ_B?Jz)G=xfg#M{XA|2VbZf<Z%uX<%#kE#Lw_8(xyKM7HO~;*lNos&
#3V}%m%YQ59?7ZSS=zSL?_78+v*$z;o-Y-PL&j?Gj~1~?`Xqi2U<fCrndGw-5=Sa{S);eY^(*t%lEv(?sa0tWRXG}=Vd;pP`y>)N
DKY4VY)Nnw#S$(3j^c=Z{C0*Iz#YX8{dhg>Fn~Os8%B-5vYq1w5Z>E1{HoZsVrngQZFmg6Yem(9?b?tCR~GO0s9wEp6vT4v=jgai
OHNK*R1BU$Va2cqYE-9R$#OlJu%rp)twK<x%Rd!Z_`*-G3%(NHg28F81K(;X`|Vl{OVijzv0=eqC>_JB;N#ADMkVp6PiW&nVq(y)
%Pf%N_q3%lxnEpZI<%7$R*Z5l7SE}q)No?9Uk*qyq4+vgfn^z!Bh8j`F&*ur*EEbz78Eh)Jo3Sbji_)m=+2GA9+4(8k9klUb>IWU
<?7aw#3Ha;*VBtPW<h-zZ7b?U7_p=ld=gvpAo21vCvZuqd4LC98Qch}y3kBy9z3LRfI%C#-gY-^M>E;)!<9=xP0*z*v-%3|B=?Ha
`v;NBtf2u`bbFfb5vkAyvG6=w&kjF+&sp$E)MA6~RSqoRO!prf)F|&Ce%XUBOZlNxK19+cAFCn*ydfMkX&G=OrJbb<Cw4C@9e))J
^0kwEhs*Kp>H^mywP3@y*y1cHO}qLgFy**$^!>Kpx*fF`xOjCqX?aZ)QVqGl3L6OY+ieqg_;ULbNFk$0YP7V6&;)6X+s%)G$>A|!
A~8IEz0>5yLmhA<@i2E?k#X3MkHsk=zFO6gK_;b!iH9gLhez5>!2RXzoB#I<1Aq9{u^=ou*PR<k3DE8R$G_dazwMzS#=Td)KFy8N
Z;h0n*_=4JIM7hy=N^PwRvkUrvp=hjB{ej|Z^>oz=I0(tni@40z54?IJ3>C8bT|}$bJ!tUPs4ns4h@<O0w!)cc>YTQS_G-68Dxlw
K(W~={V+kxYA=7!Gsv+q(++gyR5+e7ds4(eg4-k{u(efc7&@RrKzP*~#*KicY{A~q&WtfEO^ZTJ+@YFxqf5yr4^0cn?rSEHi5V2#
f9-~4OK9XDBHQNSK}xAX#GSaD6-1=JfWL_=EJI0%`pw(k%RY64>|Z0?tiW>L&D)gd7<!*Bwh~zfsw>7u7hPkHb1h}a)CLOYg{Efh
y?*<>by4vPPp}hWAn!h4CgNx+PGTWu<DUWwr`1ew+}FLQnkIQz{irGMPnT_GPn)s!b9MTBoP8#ap9OsMv(AB(IkqQ-uo02)-MiO6
-TwUDZ6Kx&Ztwn8sI{Op*tpGPqOtc|eYa<Oh<Lfv*+Q!^g?Fwew(o3v^OzyW4fYJ%rPbgVMawc%9b#01WS!)dmz<!bR{Vnz$p>4D
C4|jIf@EDv$L@l#;BFNa5*0jQF%BZVQI4oEb?Yb=*OIkG#hkSKB*BzIUlbwiTMPNfh>s(~JwD5#*h5E-iyL{`HLzWpiMrC_9@cts
reDCajjTR4$4RPHvpvnxxmllX#jCbpm4kJ1DEA%<9y}k>M$(Cib%Q4+JQ!xClTT0|-oN?r$J_UMUi(#&_g??-0lxVD_NQCePCIRW
d;8;?x12e(NRWTs!};Z}S-N?_8F@<h9H6>$@CZ?r`PbM&0{>>s{}s2tefxd*3kv!LbaY35fi_@1&@K3chll<?Vt5BSykGwMJ!<#v
2ZrI9&Gm(*=Rq-fAmw7UOCHNA$&t?!sP}v@-`@W4?)|NOS!Fr<7H`FuUlG0F?d^Yjt(rBcsffr}v_DW2-HO7viqc3FBq2=0+lCuo
Vwzz_;cdqz1r<K>x8znG-t4OP?tRZS9h#LVJS&vX1!u<FPMw{IrhJCHZH-y-wpM4#)|A}JVbxW4!Bl4|FKQy^<SFfcb@kPiM8EQW
Mt^>WJodW}z4x!*{CxZJ^|$Zde}L*^vHh5oa7c){i9wE-Qt7AlrR~{vLy>D9eM1RGBnacW=P@giSE^2IvpHH;P<kjYl>?Sk;euU{
@_ANFXdNTy7(vGfR7Ri|-?6E7$~oE`mbbW%(o#Bk^A@@qA{z~^dBGwD%vGF*LVn25@JkA@^}Lu0<0<nJ^U<mcmq&9VKV%U2<*{FS
;O!kcsC~G91XB5e)^%UsDFtZ|m)Vkg#``3%J~74FP?)7oUx_93G9S6R4ad_Xc&MR^oIA0oeTg`rubL7SL|6=!V0*W}4|TbMd~`hH
?9~VSRj;f*fjSH^2H1~3z5BM;|L?)gGPzm*&#`~s1RG4;T^GwFJs$F@e<RyHnka2Ljo^dek{Uq=t`#*v+ZWV?7XhkUPVaK+*1+Iu
3frB3F*#uGS_)`kK;25(S5l5f3a+9Yh2&a68K8aj6exZG-Kx=5!*Vi$rOMXi@Rp^mub}2|l*T|U1BOMwBOzrc1N{pu@5BX6OdZ5d
J?YewPCe<Cvr|tx^`uiz{s`)c)7zc6{}NB(Rk1<YqSMl%s%Q;~-z<It|MNgyx9vzm+6M2YIO{U_J&duptBNfu9a1zsK1x*ufzzZ9
lFiP$pT0NVF&hxvC308#HI|O#jc1Cj?(G4ak$krjsgy|uYcX??I+^aP@8%K0CBwlWI4C=J5dA$}JHEiUxvsWqF+yR@S$FVv>CGBo
ht{k^3j^ww(k_@iD|!2$Z+`ypbMGbH?Ze&=@8A8!839E7kI<6&7fT^2c!Il!CmVdT#Za@wr0+~+UT<LP7UZT=N1%CZJ8{U;O`>MK
twzKKD4BN6zRqqPrA90;Zek};GLr^D8Y|$Wk;lVB5gbE+Mw}X~1)PmeY`UwZYey?_+`>UAbS9m&BCUd*T5$9e_ajKFJqm$p9-BSb
^smms$Yf?&Z0}G=Va#F5(o`cv8!(v?tq^>jJ6e<_Y+$Wg5~OW0WNe2$B*6q1TN6cdT(n_rjHqKa;R|dBDAR`niXXsvXr%l5%l=#0
D3(BJnQk3TL@c%M{3aV80?_iqu0-kAKrFxjr7Lz18(SdRAHw{bw7cs4xi`?24YSdoAd4SaxHLs(=kKhur`x_RHuhe&kGq4@_$hfL
Su5k@l%<RJldbLH*AGVv<2!7_v3!yZ<dxTE<uj;+uhgVkaIUk`bx%?@h?lIcOYkO=Xt?fK<OS(&CDv<8wgMRjS!x2nGh1JE_M%P&
>g+|Gy{NMnb@rmpUewu(I(t!PFY4??oxP~D7j=Jk_M*;S^hdWBg%c&NNH7~S8%bF>+<ilKmR+&kf?RV~g0#A=_PrGJ2*zA7wJ>S(
l@_7uKhkO0j_|A@2POT;i00eygd$C;+tZ1YXt%vR<s_1XFVAced0?^_0;XTZ?MyhDF;6qw37<CBUg<rhbe%E={#0)-a`sEj`NLL}
Koe}n`?Z;!qwSTl6CkNK<+ob~?I((aKvvGqB=(FCk56UhI+4;NVtCBN-rY9nw!s&(4M;BHq-nsuMbx|8E0(t9KJ5d`)<w;Vl(z^(
vh{Kjw-tq*!tHP)L3XrmzXg#dD?T{M(kW(v13^kwsBr+h)xA2N--P9@ZAV@A{7cd?td3!I469>UCon8>fTjt0TdGNN-*&u5?%AFL
#nWVv5ChcbBe{;w0$jwlOu~?(X3dvP_Tu%wp!a2!CS{-7o1EZJrS-6gmh*!1F?PG5+YMjjZaCl?SW@|@7Mx%1+m6=dp6zKp$y(-M
(K<LOUk7a800y)$pl&<FZ-<U+J#Ssn&oRRCQ`;NYJ$s4Zj>U8=reiUGG!}z3pJr8k+CsucRaF9t2x%Y78A$6Mhu$gIe<T`;KiYMa
)lrs>vWgsP6R36@uXYcYeGkxuWK%Ry#mF9=R3<4rtOPM4GXrFh;%J8+?LaHB_dZpJGVi4|NFFGz9xcy`#~9~k#2-je4y>vk2GjIj
|4ab#rvK;No41lzF?`Q{pzmY<{u&b|?)kOkVXV0>k@xzXtb~tpE~QIX-IUND3f>5`LAkKlL!|>tl*JZ=cHo0%FQiUaAs?d+*rbFB
w2)&`r&yLFb!pFcT4=Umoi?P+diB^&gY7if-;<2+ZvU_f5<C}ooT4Maj#K<jI0dTZl0%hkv&zZ?R3PhyJ;nRfr!JUXTd)JOGkr?<
L-^R@s$K7)IK7LT^bFt*8Z;rs0PCpI!8{yg<-<!FW+pml|AKm%iw>$=PWy67mBw4_Qd=+QvRYBtm(~t95@ff;!6lC0w3bdW3mn+G
R;Y0RyVZ3kQg?1*cQSPsQSca5jw(u~GeoAVflXW}NZNjjfg<!NCn{GF_F#QVu6ynYkl)$9)$v|C+jVMPr`C09UALT_TGuUcr`C09
U8mOB)H+gQxy|hw@6}G7>Y+YIT<dp=RHsOFid3gab&6D{NOg+TIa9f*kuVJ?*L|LLEk*!GuuLs4rWv2(!s_KI@j9J`JDz8u)>0cb
++AIKL|NyK*(45-a)i<a6<iw$uyo6Ds;n0j`6@RgW73aTRiBsfQI2K2?&<5@Y0#Yp-D%L>a&{VYr$Kibbf-ag8g!>YUr>YoTa~tR
i$on--kPH>?e5Auzv#{{y7P-Z`_))1v$NkE9l=Y^6Oz%fosOP6w$rg42irmF?y3gs66!!GSxSA_wo_?r09x`I8;FkgbiAkIJ%4oG
b3PTQV^bZQ>ey7rrp~~oXoA*Ll%#f9SiL|x4%Bg=jstZZ=;>6Yj#+igs$*6ivx;O^`>HD8&d)%x+qct_l%6g5jncPcGaZ}h*vuc6
&74ma>Nrxzkvfjlaijo8s&Y_5<>q6RkpiLd<w}gM(M(3~*uIj0t!k~8fV*O~&pd5km=2GstK1Q3N2F&X(jBOFC5VrZC+%Y?j<oJ_
$^L-}sfG66(N{-b9{MtC3@G&!+#Jj77@{vrT?C29s#;WL*c8ns*=P3#$wih<Fv>chx(M{&M^#S1WeS{i6DZ`br9uft2?eck0_!4^
xvNlu(w<GxD@U1xQx@CKRl3Fgk{7!yk_I$wR1vvfyvQJ{je2#0qca6{rl4n9=@v(bkz&U<Vyu640uMRS27TR=N7M11j`wuDr{g^;
@2NpatZMBFdf9rn<0oq0j<5W24#NSGYi)YzD6yl&<0!G&0g&CV;Ms2tYJ6~Q%>hjiEs4(r&{1GVfu}RXjxamIJb^Ix#RjYzu;1f4
z-_sr(z_+yDSbPV>qzb-HrP>OM~N*cu{_)r+f{N0w)@ovG!6V$3r8$#_h_QCb#CwL(tvFa>Q;$GwH|Jpjt<&k+cDrpfa(bI9E2H>
c2}SHj%+)!y$snlkT}{owpzO%MVPE}d%`5q04?ZJ>)<3y9k87$4QOFN9Zhz(8^6Qb4K>3m%`bX7?+J@cZGYKsTBXr-jH_c@9pmbj
=|skrCfNgOyv<kpq`m|D)m@!z_wCgu7oe8>$OWc7XQJI(uqM5SlfDhmb~-oVMS$w)-cI*)qT;!f)*-Z*0iI<*3j^v_(!P>e4WKTR
uhLc}FKoTs5mPo4j<6{!+(?k!65E$p^1F^OON+*C!?Xm@VVahq_yKgQ<_<izN=<kCv5hPH3f=mIGHOZRN0p55NCXS0C(ww(hdTXp
uY1nn%dc#_Mt(05^L0=D&Q2Zc)Ui$->(sGM9qX33Q^z`W?02S)9qKJ9K68q3iUB!66Jj7bEvVCiIxXn;p#^oxdpc#OQ+7IK=L=AF
p3d&1vX~Q0PMwm~DOsJ8)vaWwWOYhbr(|_X)^k#_;2vKWcPprP9F7RPQF^yjZ<M|<dQBJW)wxr4?v!T>A))JZ?sV$}Y|LIY9W@UP
Dzab)(azOV0wFa(8G7!@Dox5hrohl31K*6l_jDe6a>_BQBYel2JJ#H>=8iRYtl7t!x7EH_7o^I;sygh`>bKTPumwg-ertiy8TvXy
UuWp+41JxUuQT*@hQ7|wce0@`O4E6!m#LkG)oECrhSjZPr(ty(R;OWg8rJjFu$n!@*xGu8d$rVNaG&=2OJ|n<QnR@^NULpr){f#j
iVIR4D=0^b;zbnG`$5#blj_cetLd>~b-XtL-}_V@%Dk7>AbB7~_8NuGB1`Dm3jCoOdH7#y4Lx7AJ@iRRc|6?TTOf}NleAOS4^mn$
R2h$nLR3vpsf>sJ3pY^Y3=sLo2e`ch&E7u|N{@(H&@kAifFl0<GZ8}yPcj2%{dD)vm20Z3z(;`6G(=~Sw{(sb5tgo9=I%d$q==%o
mMTg|h8-E6LWVd-=s2d=CPK&P*il+XX{WNgjsQCX3=!b2d|ZL`8f1GWaii@`md^3iW^`?D3+RmWe?)2wGemvXJ5udP_3Wg2*y74k
)UmySb5|F6n}8=5glM{S^zXduJMa3=yT0?T|9yGa$Mf4W2S0Uex?|HFo9@_j$EMq`X`EiDO5xXiBvz&Nda(hbEypzgbS$@HxgE>>
qqAJ@$ag-j+u3^l5VoEOq1M_sd41}+q!W{!nC!%4Cnh^F*@?-|D<&;Ik&3Uc>%eE}7m{JPOOJFRE|HSALRAX~;7!qNl6`h>h_GWk
US;o1)yb2)D5DnK4O#MC{+c^#O0JulYp3E2iuaPs6;$=`l(?|y74`>xB?cGk4;orwyY|zq@Rzp2RuAtMd)eAQZpE2%H0erL{uwW}
>5G1*HNV6v`uA(MiqB{j+XT;uBcmKfqk?O<j+QtY(%|@Ln)b0Ik?ZM-5wQkR=*#GerjVm1n2zmO&?k!|Y4!c*`tX<1@1D|EI&9cw
QjeVp;wFy#xeD(J0J8hl9e~O}+p7h$Z>+Jz?AiH%bqAgZQas9Av7^(DPR~fE`>HA-PyASAZ5gKCGnNeXejO2ZL>MH(qgZ7}p&f;u
i9$btbk%^QhVrvMwBeFk$5;~7x^)!TQDBGyk7AJ>d3NO4o;(?Qd*6G1``4dde}@>}hj+c^*Rt3HNEp8!&=LOSHrasd9{hRL`}fyB
{c`(r?`2<^R(v7Y^sjrPtCN6kz^1AnSL-C(v*blM77IciK*-;{`^#V6eE8R|{`&s)kAHdHTURwG?zXE3@YuY1_x4}^e>YarpRpPQ
00
"""

# Redacted sqlite_master SQL from the deployed 0048 database. The live database
# was opened read-only; no rows, account identifiers, or message content are
# embedded here. Keeping the exact DDL makes the second accepted digest as
# strict as the frozen fixture digest.
_PRODUCTION_0048_CHAT_EVENTS_DDL = """CREATE TABLE "chat_events" (
    id INTEGER NOT NULL,
    bot_user_id VARCHAR(64) NOT NULL,
    platform_message_id VARCHAR(128) NOT NULL,
    scope_type VARCHAR(16) NOT NULL,
    group_id VARCHAR(64),
    private_peer_user_id VARCHAR(64),
    sender_user_id VARCHAR(64) NOT NULL,
    direction VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    reply_to_message_id VARCHAR(128),
    occurred_at DATETIME NOT NULL,
    observed_at DATETIME NOT NULL,
    visual_summary TEXT DEFAULT '' NOT NULL,
    origin VARCHAR(32) DEFAULT 'user_message' NOT NULL,
    automation_id INTEGER,
    automation_run_id INTEGER,
    event_kind VARCHAR(32) DEFAULT 'message' NOT NULL,
    source_plugin_id VARCHAR(128),
    external_source VARCHAR(64),
    external_event_key VARCHAR(255),
    external_event_type VARCHAR(128),
    external_payload_json TEXT,
    external_target_id VARCHAR(64),
    sender_nickname VARCHAR(128) DEFAULT '' NOT NULL,
    sender_group_card VARCHAR(128) DEFAULT '' NOT NULL,
    canonical_event_id VARCHAR(36),
    canonical_conversation_id VARCHAR(36)
        REFERENCES canonical_conversations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    author_kind VARCHAR(16),
    author_person_id VARCHAR(36)
        REFERENCES persons(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    author_presence_id VARCHAR(36)
        REFERENCES presences(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    ingress_presence_id VARCHAR(36)
        REFERENCES presences(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    utterance_fingerprint VARCHAR(64),
    suppression_status VARCHAR(16),
    ingress_provider VARCHAR(32),
    ingress_gateway_instance_id VARCHAR(128),
    PRIMARY KEY (id)
)"""

_CARRIER_PERSON_IDS = (
    "e8b15d59-3988-473e-a13c-d277ca77b5c1",
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)


class _SyntheticFailpoint(RuntimeError):
    pass


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config("alembic.ini")


def _upgrade(path: Path, monkeypatch: pytest.MonkeyPatch, revision: str = "head") -> None:
    command.upgrade(_config(path, monkeypatch), revision)


def _restore_historical_0048(path: Path) -> None:
    encoded = "".join(_HISTORICAL_0048_B85.split())
    script = gzip.decompress(base64.b85decode(encoded)).decode("utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(script)


def _rewrite_chat_events_as_production_0048(connection: sqlite3.Connection) -> None:
    objects = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='chat_events' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type, name"
        )
    )
    columns = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info('chat_events')"))
    projection = ", ".join(f'"{column}"' for column in columns)
    temporary = "__production_0048_chat_events"
    ddl = _PRODUCTION_0048_CHAT_EVENTS_DDL.replace(
        'CREATE TABLE "chat_events"',
        f'CREATE TABLE "{temporary}"',
        1,
    )

    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(ddl)
    connection.execute(
        f'INSERT INTO "{temporary}" ({projection}) SELECT {projection} FROM "chat_events"'
    )
    connection.execute('DROP TABLE "chat_events"')
    connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "chat_events"')
    for statement in objects:
        connection.execute(statement)


def _restore_production_historical_0048(path: Path) -> None:
    _restore_historical_0048(path)
    with sqlite3.connect(path) as connection:
        _rewrite_chat_events_as_production_0048(connection)


def _seed_production_alias_carriers(path: Path) -> None:
    now = "2026-08-26T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE person_aliases SET user_id=canonical_person_id "
            "WHERE canonical_person_id=? AND group_scope=''",
            (_CARRIER_PERSON_IDS[0],),
        )
        for index, person_id in enumerate(_CARRIER_PERSON_IDS[1:], start=2):
            connection.execute(
                "INSERT INTO persons "
                "(id, enabled, revision, created_at, updated_at) VALUES (?, 1, 1, ?, ?)",
                (person_id, now, now),
            )
            connection.execute(
                "INSERT INTO identity_bindings "
                "(id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at) VALUES (?, ?, 'qq', ?, ?, 'active', 1, ?, ?)",
                (
                    f"{index + 2}{index + 2}{index + 2}{index + 2}{index + 2}{index + 2}"
                    f"{index + 2}{index + 2}-{index + 2}{index + 2}{index + 2}{index + 2}"
                    f"-4{index + 2}{index + 2}{index + 2}-8{index + 2}{index + 2}{index + 2}"
                    f"-{str(index + 2) * 12}",
                    person_id,
                    f"fixture-account-{index}",
                    f"Fixture Person {index}",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO person_aliases "
                "(user_id, group_scope, alias, alias_type, first_seen_at, last_seen_at, "
                "canonical_person_id, canonical_space_id) "
                "VALUES (?, '', ?, 'nickname', ?, ?, ?, NULL)",
                (person_id, f"canonical carrier {index}", now, now, person_id),
            )


def _seed_inverted_identity_metadata(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE people SET first_seen_at='2026-08-30', last_seen_at='2026-08-10' "
            "WHERE canonical_person_id=?",
            (_CARRIER_PERSON_IDS[0],),
        )
        connection.execute(
            "UPDATE identity_bindings SET created_at='2026-08-05', "
            "updated_at='2026-08-20' WHERE person_id=?",
            (_CARRIER_PERSON_IDS[0],),
        )
        connection.execute(
            "UPDATE persons SET created_at='2026-08-25', updated_at='2026-08-15' WHERE id=?",
            (_CARRIER_PERSON_IDS[0],),
        )
        connection.execute(
            "UPDATE identity_bindings SET created_at='2026-08-20', "
            "updated_at='2026-08-05' WHERE person_id=?",
            (_CARRIER_PERSON_IDS[1],),
        )
        connection.execute(
            "UPDATE persons SET created_at='2026-08-15', updated_at='2026-08-10' WHERE id=?",
            (_CARRIER_PERSON_IDS[1],),
        )
        connection.execute(
            "UPDATE groups SET first_seen_at='2026-08-30', last_seen_at='2026-08-10', "
            "updated_at='2026-08-25' WHERE canonical_space_id=?",
            ("6439f510-e073-4c3d-8d51-106d3c0b7ee5",),
        )
        connection.execute(
            "UPDATE space_bindings SET created_at='2026-08-20', updated_at='2026-08-05' "
            "WHERE space_id=?",
            ("6439f510-e073-4c3d-8d51-106d3c0b7ee5",),
        )
        connection.execute(
            "UPDATE spaces SET created_at='2026-08-25', updated_at='2026-08-15' WHERE id=?",
            ("6439f510-e073-4c3d-8d51-106d3c0b7ee5",),
        )


def _seed_event_automation_references(path: Path) -> None:
    now = "2026-08-26T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO automation_runs "
            "(id, automation_id, scheduled_for, actual_started_at, finished_at, status, "
            "idempotency_key, steps_completed, llm_calls, tool_calls, messages_sent, "
            "error_category, result_summary_json, created_at) "
            "VALUES (1, 1, ?, ?, ?, 'succeeded', 'fixture-valid-run', 1, 0, 0, 1, "
            "NULL, '{}', ?)",
            (now, now, now, now),
        )
        connection.execute("UPDATE chat_events SET automation_id=1, automation_run_id=1 WHERE id=1")
        connection.execute(
            "UPDATE chat_events SET automation_id=999001, automation_run_id=999002 WHERE id=2"
        )


def _load_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_revision_0049", _BRIDGE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load 0049 revision")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge_schema_digest(path: Path) -> str:
    bridge = _load_bridge()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            return str(bridge._schema_digest(connection))
    finally:
        engine.dispose()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _logical_digest(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        dump = "\n".join(connection.iterdump())
    return hashlib.sha256(dump.encode()).hexdigest()


def _schema_shape(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        tables = sorted(_tables(connection))
        return {
            "tables": tables,
            "columns": {
                table: [
                    tuple(row[1:]) for row in connection.execute(f'PRAGMA table_xinfo("{table}")')
                ]
                for table in tables
            },
            "foreign_keys": {
                table: sorted(
                    tuple(row[2:])
                    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
                )
                for table in tables
            },
            "indexes": {
                table: sorted(
                    (
                        "auto" if str(row[1]).startswith("sqlite_autoindex_") else str(row[1]),
                        int(row[2]),
                        str(row[3]),
                        int(row[4]),
                        tuple(
                            item[2]
                            for item in connection.execute(f'PRAGMA index_xinfo("{row[1]}")')
                            if int(item[5]) == 1
                        ),
                    )
                    for row in connection.execute(f'PRAGMA index_list("{table}")')
                )
                for table in tables
            },
            "triggers": sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ),
        }


def _assert_orm_shape(path: Path) -> None:
    from qq_ai_bot.persistence.metadata import Base

    with sqlite3.connect(path) as connection:
        physical_tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not name.startswith("sqlite_")
            and name != "alembic_version"
            and not name.startswith("chat_events_fts")
            and not name.startswith("memory_facts_fts")
        }
        assert physical_tables == set(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            physical_columns = {
                str(row[1]): (str(row[2]).upper(), bool(row[3]), bool(row[5]))
                for row in connection.execute(f'PRAGMA table_info("{name}")')
            }
            model_columns = {
                column.name: (
                    str(column.type).upper(),
                    not column.nullable,
                    bool(column.primary_key),
                )
                for column in table.columns
            }
            assert physical_columns == model_columns

            physical_foreign_keys = {
                (
                    str(row[3]),
                    str(row[2]),
                    str(row[4]),
                    str(row[5]).upper(),
                    str(row[6]).upper(),
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')
            }
            model_foreign_keys = {
                (
                    foreign_key.parent.name,
                    foreign_key.column.table.name,
                    foreign_key.column.name,
                    str(foreign_key.constraint.onupdate or "NO ACTION").upper(),
                    str(foreign_key.constraint.ondelete or "NO ACTION").upper(),
                )
                for foreign_key in table.foreign_keys
            }
            assert physical_foreign_keys == model_foreign_keys


def _assert_final_health(path: Path, *, populated: bool) -> None:
    with sqlite3.connect(path) as connection:
        from qq_ai_bot.persistence.schema_guard import canonical_schema_revision

        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            canonical_schema_revision(),
        )
        tables = _tables(connection)
        assert not (_RETIRED_TABLES & tables)
        assert {
            "persons",
            "spaces",
            "presences",
            "canonical_conversations",
            "canonical_event_receipts",
        } <= tables
        receipt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('memory_recall_receipts')")
        }
        assert {
            "consumer",
            "attribution_status",
            "tool_read_success_count",
            "tool_read_infrastructure_failure_count",
        } <= receipt_columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND (name LIKE 'chat_events_fts_%' OR name LIKE 'memory_facts_fts_%')"
            )
        }
        assert triggers == _FTS_TRIGGERS
        connection.execute(
            "INSERT INTO chat_events_fts(chat_events_fts, rank) VALUES ('integrity-check', 1)"
        )
        connection.execute(
            "INSERT INTO memory_facts_fts(memory_facts_fts, rank) VALUES ('integrity-check', 1)"
        )
        if populated:
            assert connection.execute(
                "SELECT rowid FROM chat_events_fts WHERE chat_events_fts MATCH ?",
                ('"historical"',),
            ).fetchall() == [(1,), (2,)]
            connection.execute(
                "UPDATE memory_facts SET content='migration memory token' WHERE id=1"
            )
            assert connection.execute(
                "SELECT rowid FROM memory_facts_fts WHERE memory_facts_fts MATCH ?",
                ('"migration"',),
            ).fetchall() == [(1,)]


def _apply_preflight_case(connection: sqlite3.Connection, case: str) -> None:
    if case == "v1":
        connection.execute(
            "UPDATE identity_runtime_state SET state='v1', cutover_id=NULL, "
            "source_fingerprint=NULL, completed_at=NULL WHERE id=1"
        )
    elif case == "conflict":
        connection.execute(
            "INSERT INTO identity_conflicts "
            "(platform, external_id, subject_kind, conflict_kind, status, "
            "error_category, resolved_at, created_at, updated_at) VALUES "
            "('qq', 'fixture-conflict', 'account', 'ambiguous_identity', "
            "'open', 'fixture', NULL, '2026-08-26', '2026-08-26')"
        )
    elif case == "lease":
        connection.execute("UPDATE memory_jobs SET status='processing' WHERE id=1")
    elif case == "ownership":
        connection.execute("UPDATE memory_jobs SET canonical_space_id=NULL WHERE id=1")
    else:
        raise AssertionError(f"unknown preflight case: {case}")


def test_only_two_explicit_historical_0048_schema_digests_are_accepted(tmp_path: Path) -> None:
    bridge = _load_bridge()
    fixture = tmp_path / "fixture-0048.db"
    production = tmp_path / "production-0048.db"
    _restore_historical_0048(fixture)
    _restore_production_historical_0048(production)

    observed = {_bridge_schema_digest(fixture), _bridge_schema_digest(production)}
    assert observed == bridge._HISTORICAL_SCHEMA_DIGESTS
    assert observed == {
        "235100f1310f0362bdcfecd13124da7f7c729bfbfd760c6b9233e0128a9f8168",
        "11e87cc3e57199863be5ee6bfe8fb72ab90a070bc1253cf5475825fb6cc978b2",
    }


def test_fresh_baseline_reaches_current_head_with_final_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from qq_ai_bot.conversation.projection_schema import PROJECTION_TRIGGERS_0054
    from qq_ai_bot.persistence.schema_guard import CanonicalSchemaError, require_canonical_schema

    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_bases() == ["0048"]
    assert len(scripts.get_heads()) == 1

    path = tmp_path / "fresh.db"
    _upgrade(path, monkeypatch)
    _assert_final_health(path, populated=False)
    _assert_orm_shape(path)

    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    asyncio.run(require_canonical_schema(url))
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER prompt_projection_reset")
    with pytest.raises(CanonicalSchemaError, match="projection invalidation trigger"):
        asyncio.run(require_canonical_schema(url))
    with sqlite3.connect(path) as connection:
        connection.execute(PROJECTION_TRIGGERS_0054["prompt_projection_reset"])
    asyncio.run(require_canonical_schema(url))

    old = tmp_path / "0050.db"
    _upgrade(old, monkeypatch, "0050")
    with sqlite3.connect(old) as connection:
        connection.execute(
            "INSERT INTO memory_recall_receipts "
            "(turn_id,origin,mode,purpose,candidate_count,selected_count,injected_count,"
            "used_count,reinforced_count,created_at,updated_at,expires_at) VALUES "
            "('old','user_message','hybrid','background',0,0,0,0,0,"
            "'2026-01-01','2026-01-01','2026-02-01')"
        )
    _upgrade(old, monkeypatch, "0051")
    with sqlite3.connect(old) as connection:
        recall_before = connection.execute("SELECT * FROM memory_recall_receipts").fetchall()
        assert "social_operation_receipts" not in _tables(connection)
    _upgrade(old, monkeypatch)
    _assert_orm_shape(old)
    with sqlite3.connect(old) as connection:
        assert (
            connection.execute("SELECT * FROM memory_recall_receipts").fetchall() == recall_before
        )
        assert connection.execute("SELECT count(*) FROM social_operation_receipts").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT turn_id, attribution_status, attribution_completed_at "
            "FROM memory_recall_receipts"
        ).fetchone() == ("old", "unknown", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    # Future migration heads must be discovered without editing the startup guard.
    import shutil

    from qq_ai_bot.persistence.schema_guard import canonical_schema_revision

    bundle = tmp_path / "bundle"
    shutil.copytree(Path("migrations"), bundle / "migrations")
    next_revision = bundle / "migrations/versions/future.py"
    current_head = canonical_schema_revision()
    next_revision.write_text(
        f"revision = 'future'\ndown_revision = '{current_head}'\n", encoding="utf-8"
    )
    assert canonical_schema_revision(bundle) == "future"
    monkeypatch.chdir(bundle)
    with pytest.raises(CanonicalSchemaError, match="future"):
        asyncio.run(require_canonical_schema(url))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'future'")
    asyncio.run(require_canonical_schema(url))
    (bundle / "migrations/versions/branch.py").write_text(
        f"revision = 'branch'\ndown_revision = '{current_head}'\n", encoding="utf-8"
    )
    with pytest.raises(CanonicalSchemaError, match="one bundled migration head"):
        asyncio.run(require_canonical_schema(url))


@pytest.mark.parametrize(
    "restore_historical",
    (_restore_historical_0048, _restore_production_historical_0048),
    ids=("frozen-fixture", "deployed-production-ddl"),
)
def test_populated_historical_0048_preserves_data_and_matches_fresh_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_historical: Callable[[Path], None],
) -> None:
    path = tmp_path / "historical.db"
    fresh = tmp_path / "fresh.db"
    restore_historical(path)
    # Parent-table rebuilds must retain CASCADE children and SET NULL edges,
    # not merely preserve fact bodies and pass foreign_key_check.
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(memory_facts)")]
        projection = ", ".join(
            "2" if column == "id" else "'k2'" if column == "memory_key" else f'"{column}"'
            for column in columns
        )
        connection.execute(f"INSERT INTO memory_facts SELECT {projection} FROM memory_facts")
        connection.execute("UPDATE memory_facts SET supersedes_id=1 WHERE id=2")
        connection.execute(
            "INSERT INTO memory_fact_state_events "
            "(fact_id, action, reason_code, source_event_id, created_at) "
            "VALUES (1, 'superseded', 'migration_regression', 1, '2026-08-26')"
        )
        connection.execute(
            "INSERT INTO memory_fact_relations "
            "(source_fact_id,target_fact_id,relation_type,confidence,source_event_id,created_at) "
            "VALUES (2,1,'refines',1.0,1,'2026-08-26')"
        )
        connection.execute(
            "INSERT INTO memory_evidence "
            "(fact_id,event_id,source_speaker_user_id,relation,confidence,"
            "authority,excerpt,created_at) "
            "VALUES (2,1,'1001','self_statement',1.0,'self_report',"
            "'historical group message','2026-08-26')"
        )
        retained_children = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            for table in ("memory_fact_state_events", "memory_fact_relations", "memory_evidence")
        }
    _upgrade(path, monkeypatch)
    _upgrade(fresh, monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT platform_message_id, sender_user_id, group_id, content "
            "FROM chat_events ORDER BY id"
        ).fetchall() == [
            ("group-history", "1001", "2001", "historical group message"),
            ("private-history", "1001", None, "historical private message"),
        ]
        assert connection.execute(
            "SELECT memory_key, content, canonical_subject_person_id FROM memory_facts ORDER BY id"
        ).fetchall() == [
            ("k", "c", "e8b15d59-3988-473e-a13c-d277ca77b5c1"),
            ("k2", "c", "e8b15d59-3988-473e-a13c-d277ca77b5c1"),
        ]
        assert connection.execute(
            "SELECT supersedes_id FROM memory_facts WHERE id=2"
        ).fetchone() == (1,)
        for table, expected in retained_children.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall() == expected
        assert connection.execute(
            "SELECT enabled FROM persons WHERE id='e8b15d59-3988-473e-a13c-d277ca77b5c1'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT name, enabled, autonomous_enabled, require_mention FROM spaces "
            "WHERE id='6439f510-e073-4c3d-8d51-106d3c0b7ee5'"
        ).fetchone() == ("Current Space", 0, 0, 0)
        assert connection.execute(
            "SELECT alias, alias_type FROM person_aliases ORDER BY alias"
        ).fetchall() == [
            ("group card", "group_card"),
            ("known alias", "nickname"),
            ("old nickname", "nickname"),
        ]
        assert connection.execute(
            "SELECT status, canonical_creator_person_id, canonical_target_person_id, "
            "canonical_target_space_id FROM automations ORDER BY id"
        ).fetchall() == [
            (
                "active",
                "e8b15d59-3988-473e-a13c-d277ca77b5c1",
                None,
                "6439f510-e073-4c3d-8d51-106d3c0b7ee5",
            ),
            ("completed", None, None, None),
        ]
        assert connection.execute("SELECT COUNT(*) FROM memberships").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM plugin_state").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM emoji_scope_states").fetchone() == (1,)

    _assert_final_health(path, populated=True)
    assert _schema_shape(path) == _schema_shape(fresh)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("v1", "state_mismatch"),
        ("conflict", "identity_conflicts"),
        ("lease", "lease_not_drained"),
        ("ownership", "canonical_owner_incomplete"),
    ],
)
def test_historical_preflight_rejects_without_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    path = tmp_path / f"reject-{case}.db"
    _restore_historical_0048(path)
    with sqlite3.connect(path) as connection:
        _apply_preflight_case(connection, case)
        connection.commit()
    before = _logical_digest(path)

    with pytest.raises(Exception, match=reason):
        _upgrade(path, monkeypatch)

    assert _logical_digest(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0048",)


@pytest.mark.parametrize(
    "restore_historical",
    (_restore_historical_0048, _restore_production_historical_0048),
    ids=("frozen-fixture", "deployed-production-ddl"),
)
def test_historical_schema_manifest_rejects_unknown_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_historical: Callable[[Path], None],
) -> None:
    path = tmp_path / "unknown-ddl.db"
    restore_historical(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE persons ADD COLUMN injected TEXT")
        connection.commit()
    before = _logical_digest(path)

    with pytest.raises(Exception, match="historical_schema_manifest_mismatch"):
        _upgrade(path, monkeypatch)

    assert _logical_digest(path) == before


def test_production_global_canonical_alias_carriers_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "production-alias-carriers.db"
    _restore_production_historical_0048(path)
    _seed_production_alias_carriers(path)

    _upgrade(path, monkeypatch)

    with sqlite3.connect(path) as connection:
        aliases = set(
            connection.execute(
                "SELECT canonical_person_id, alias, canonical_space_id "
                "FROM person_aliases WHERE canonical_person_id IN (?, ?, ?)",
                _CARRIER_PERSON_IDS,
            ).fetchall()
        )
        assert {
            (_CARRIER_PERSON_IDS[0], "known alias", None),
            (_CARRIER_PERSON_IDS[1], "canonical carrier 2", None),
            (_CARRIER_PERSON_IDS[2], "canonical carrier 3", None),
        } <= aliases
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_bindings "
            "WHERE person_id IN (?, ?, ?) AND status='active'",
            _CARRIER_PERSON_IDS,
        ).fetchone() == (3,)
    _assert_final_health(path, populated=True)


def test_inverted_legacy_metadata_is_merged_as_a_lossless_time_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "inverted-identity-metadata.db"
    _restore_production_historical_0048(path)
    _seed_production_alias_carriers(path)
    _seed_inverted_identity_metadata(path)

    _upgrade(path, monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT date(first_seen_at), date(last_seen_at) FROM identity_bindings "
            "WHERE person_id=?",
            (_CARRIER_PERSON_IDS[0],),
        ).fetchone() == ("2026-08-05", "2026-08-30")
        assert connection.execute(
            "SELECT date(created_at), date(updated_at) FROM persons WHERE id=?",
            (_CARRIER_PERSON_IDS[0],),
        ).fetchone() == ("2026-08-05", "2026-08-30")
        assert connection.execute(
            "SELECT date(first_seen_at), date(last_seen_at) FROM identity_bindings "
            "WHERE person_id=?",
            (_CARRIER_PERSON_IDS[1],),
        ).fetchone() == ("2026-08-05", "2026-08-20")
        assert connection.execute(
            "SELECT date(created_at), date(updated_at) FROM persons WHERE id=?",
            (_CARRIER_PERSON_IDS[1],),
        ).fetchone() == ("2026-08-05", "2026-08-20")
        assert connection.execute(
            "SELECT date(first_seen_at), date(last_seen_at) FROM space_bindings WHERE space_id=?",
            ("6439f510-e073-4c3d-8d51-106d3c0b7ee5",),
        ).fetchone() == ("2026-08-05", "2026-08-30")
        assert connection.execute(
            "SELECT date(created_at), date(updated_at) FROM spaces WHERE id=?",
            ("6439f510-e073-4c3d-8d51-106d3c0b7ee5",),
        ).fetchone() == ("2026-08-05", "2026-08-30")
        assert connection.execute(
            "SELECT COUNT(*) FROM person_aliases WHERE first_seen_at > last_seen_at"
        ).fetchone() == (0,)
    _assert_final_health(path, populated=True)


def test_unconstrained_historical_event_automation_references_follow_set_null_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "historical-event-provenance.db"
    _restore_production_historical_0048(path)
    _seed_event_automation_references(path)
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT id, origin, canonical_event_id FROM chat_events ORDER BY id"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _upgrade(path, monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT id, automation_id, automation_run_id FROM chat_events ORDER BY id"
        ).fetchall() == [(1, 1, 1), (2, None, None)]
        assert (
            connection.execute(
                "SELECT id, origin, canonical_event_id FROM chat_events ORDER BY id"
            ).fetchall()
            == before
        )
        assert connection.execute("SELECT COUNT(*) FROM chat_events").fetchone() == (2,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    _assert_final_health(path, populated=True)


@pytest.mark.parametrize(
    "case",
    ("person-missing", "binding-missing", "canonical-mismatch", "group-space-mismatch"),
)
def test_canonical_alias_carrier_crosswalk_rejects_invalid_owner(
    tmp_path: Path,
    case: str,
) -> None:
    path = tmp_path / f"invalid-alias-carrier-{case}.db"
    _restore_production_historical_0048(path)
    _seed_production_alias_carriers(path)
    with sqlite3.connect(path) as connection:
        if case == "person-missing":
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM identity_bindings WHERE person_id=?",
                (_CARRIER_PERSON_IDS[1],),
            )
            connection.execute(
                "DELETE FROM persons WHERE id=?",
                (_CARRIER_PERSON_IDS[1],),
            )
        elif case == "binding-missing":
            connection.execute(
                "DELETE FROM identity_bindings WHERE person_id=?",
                (_CARRIER_PERSON_IDS[1],),
            )
        elif case == "canonical-mismatch":
            connection.execute(
                "UPDATE person_aliases SET user_id=? WHERE canonical_person_id=?",
                (_CARRIER_PERSON_IDS[2], _CARRIER_PERSON_IDS[1]),
            )
        elif case == "group-space-mismatch":
            connection.execute(
                "UPDATE person_aliases SET user_id='1001', group_scope='fixture-wrong-space', "
                "canonical_space_id='6439f510-e073-4c3d-8d51-106d3c0b7ee5' "
                "WHERE canonical_person_id=?",
                (_CARRIER_PERSON_IDS[0],),
            )
        else:
            raise AssertionError(f"unknown carrier case: {case}")

    bridge = _load_bridge()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            with pytest.raises(bridge.CanonicalBridgeError, match="canonical_crosswalk_mismatch"):
                bridge._require_legacy_crosswalks(connection)
    finally:
        engine.dispose()


def test_historical_semantic_forgery_and_crosswalks_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corruptions = (
        (
            "missing-manifest",
            "DELETE FROM identity_cutover_manifests",
            "state_mismatch",
        ),
        (
            "missing-apply-run",
            "DELETE FROM identity_cutover_runs WHERE mode='apply'",
            "state_mismatch",
        ),
        (
            "orphan-receipt",
            "UPDATE canonical_event_receipts SET "
            "canonical_event_id='11111111-1111-4111-8111-111111111111' WHERE id=1",
            "canonical_event_incomplete",
        ),
        (
            "event-without-keeper",
            "UPDATE chat_events SET suppression_status='duplicate', "
            "utterance_fingerprint="
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' "
            "WHERE id=1",
            "canonical_event_incomplete",
        ),
        (
            "group-session-owner-loss",
            "UPDATE plugin_agent_sessions SET canonical_owner_person_id=NULL "
            "WHERE scope_type='group'",
            "canonical_crosswalk_mismatch",
        ),
        (
            "outbox-wrong-conversation",
            "UPDATE plugin_notification_outbox SET status='pending', "
            "canonical_target_space_id='6439f510-e073-4c3d-8d51-106d3c0b7ee5', "
            "canonical_conversation_id='c588edad-e373-48d1-b4f6-5a7b32ae540e', "
            "canonical_presence_id='b82eb009-d855-4a7e-9ddd-b2d70975d270' WHERE id=1",
            "canonical_owner_incomplete",
        ),
        (
            "background-job-wrong-conversation",
            "UPDATE plugin_background_turn_jobs SET status='pending', "
            "canonical_target_space_id='6439f510-e073-4c3d-8d51-106d3c0b7ee5', "
            "canonical_conversation_id='c588edad-e373-48d1-b4f6-5a7b32ae540e', "
            "canonical_presence_id='b82eb009-d855-4a7e-9ddd-b2d70975d270' WHERE id=1",
            "canonical_owner_incomplete",
        ),
    )
    for name, statement, reason in corruptions:
        path = tmp_path / f"semantic-{name}.db"
        _restore_historical_0048(path)
        with sqlite3.connect(path) as connection:
            connection.execute(statement)
            connection.commit()
        before = _logical_digest(path)

        with pytest.raises(Exception, match=reason):
            _upgrade(path, monkeypatch)

        assert _logical_digest(path) == before
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0048",
            )


def test_bridge_failpoints_roll_back_every_destructive_phase(tmp_path: Path) -> None:
    bridge = _load_bridge()
    phases = (
        "after_preflight",
        "after_identity_merge",
        "after_table_rebuild",
        "after_retired_drop",
    )
    for phase in phases:
        path = tmp_path / f"failpoint-{phase}.db"
        _restore_historical_0048(path)
        before = _logical_digest(path)

        def trip(name: str, *, expected: str = phase) -> None:
            if name == expected:
                raise _SyntheticFailpoint(name)

        bridge._FAILPOINT = trip
        engine = create_engine(f"sqlite:///{path.as_posix()}")
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(_SyntheticFailpoint, match=phase):
                bridge._upgrade_historical_0048(connection, bridge._tables(connection))
            connection.rollback()
        engine.dispose()

        assert _logical_digest(path) == before
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0048",
            )
