# tmpnn
Graph based deep learning method to design protein sequences.There are twon main models which are TMPNN-alpha and TMPNN-beta. 
1. TMPNN-alpha is only used for inverse folding (also called fixed backbone design).
2. TMPNN-beta is trained to not only capture the residue identity information but the residue topology information. We add a conditional random field after the encoder block.