# Third-party notices

## city2graph

`fifteen/core.py` contains `waxman_network()`, a reimplementation of the network-distance
Waxman graph in city2graph's `waxman_graph()`. It uses the same connection probability,
random-number stream and node snapping, so a given seed returns the same edges.
`pipelines/validate.py` checks this against the installed package.

    BSD 3-Clause License
    
    Copyright (c) 2025-2026, Yuta Sato & City2Graph developers
    
    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:
    
    1. Redistributions of source code must retain the above copyright notice, this
       list of conditions and the following disclaimer.
    
    2. Redistributions in binary form must reproduce the above copyright notice,
       this list of conditions and the following disclaimer in the documentation
       and/or other materials provided with the distribution.
    
    3. Neither the name of the copyright holder nor the names of its
       contributors may be used to endorse or promote products derived from
       this software without specific prior written permission.
    
    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
    DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
    FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
    DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
    SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
    CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
    OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
    OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## Data

- OpenStreetMap data © OpenStreetMap contributors, available under the Open Database
  Licence 1.0 (https://opendatacommons.org/licenses/odbl/). Read here through Esri's
  hosted OpenStreetMap feature layers. Data derived from it in this repository is
  published under the same licence.
- Local authority boundaries and the Index of Place Names: Source: Office for National
  Statistics licensed under the Open Government Licence v3.0. Contains OS data © Crown
  copyright and database right 2025.
- Satellite imagery in the map: Powered by Esri. Esri, Maxar, Earthstar Geographics, and the GIS User
  Community. Loaded from Esri's World Imagery service at view time and not redistributed here.
